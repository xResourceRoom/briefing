"""Daily job: fetch ASX closing prices (EODHD), write the bulletin script, make the voice audio, build ./site.
Env: EODHD_KEY (secret). Testing only: EODHD_FAKE=1 (made-up prices), TTS=off|fake. VOICE (default bm_george)."""
import os, json, re, sys, shutil, subprocess, datetime as dt, urllib.request, urllib.parse, math, wave, email.utils, xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
SYD = ZoneInfo('Australia/Sydney'); ROOT = os.path.dirname(os.path.abspath(__file__)); SITE = os.path.join(ROOT, 'site')
try: SECRETS = json.loads(os.environ.get('SECRETS_JSON') or '{}')
except Exception: SECRETS = {}
KEY = os.environ.get('EODHD_KEY') or SECRETS.get('EODHD_KEY', ''); FAKE = os.environ.get('EODHD_FAKE') == '1'; TTS = os.environ.get('TTS', 'on'); VOICE = os.environ.get('VOICE', 'bm_george')
NEWS_FAKE = os.environ.get('NEWS_FAKE') == '1'; MODEL = os.environ.get('NEWS_MODEL', 'openai/gpt-4.1-mini'); WRITER_MODEL = os.environ.get('WRITER_MODEL', 'openai/gpt-4.1'); GH_TOKEN = os.environ.get('GITHUB_TOKEN') or SECRETS.get('github_token', ''); TAVILY_KEY = os.environ.get('TAVILY_KEY') or SECRETS.get('TAVILY_KEY', ''); GEMINI_KEY = os.environ.get('GEMINI_KEY') or SECRETS.get('GEMINI_KEY', '')

def watchlist():
    out = []
    for line in open(os.path.join(ROOT, 'watchlist.txt'), encoding='utf-8'):
        line = line.split('#')[0].strip()
        if not line: continue
        t, _, n = line.partition(',')
        t = re.sub(r'[^A-Za-z0-9.\-]', '', t).upper()
        if t: out.append((t, n.strip() or t))
    return out[:20]

now = dt.datetime.now(SYD)
cutoff = now.date() if (now.weekday() < 5 and now.hour >= 16) else now.date() - dt.timedelta(days=1)   # only count a day once the ASX has closed

def fetch(t):
    start = (now.date() - dt.timedelta(days=400)).isoformat(); end = cutoff.isoformat()
    if FAKE:
        rows, d, i = [], now.date() - dt.timedelta(days=400), 0
        while d <= cutoff:
            if d.weekday() < 5: rows.append([d.isoformat(), round(40 + 5 * math.sin(i / 9 + len(t)) + i * 0.01, 3)]); i += 1
            d += dt.timedelta(days=1)
        return rows
    q = urllib.parse.urlencode({'from': start, 'to': end, 'fmt': 'json', 'period': 'd', 'api_token': KEY})
    with urllib.request.urlopen(f'https://eodhd.com/api/eod/{t}.AU?{q}', timeout=60) as r:
        data = json.load(r)
    return [[o['date'], float(o['close'])] for o in data if o.get('date') and float(o.get('close') or 0) > 0 and o['date'] <= end]

def chg(rows, days):
    if len(rows) < 2: return None
    last = rows[-1]
    if days == 1: ref = rows[-2]
    else:
        tg = (dt.date.fromisoformat(last[0]) - dt.timedelta(days=days)).isoformat()
        ref = next((r for r in reversed(rows) if r[0] <= tg), None)
        if not ref: return None
    return (last[1] - ref[1]) / ref[1] * 100

def spoken(s):   # read initialisms letter by letter (BHP, ANZ), a few as words
    return re.sub(r'\b[A-Z]{2,4}\b', lambda m: m.group(0) if m.group(0) in ('NAB', 'REA') else ' '.join(m.group(0)), s)

def scenes(stocks, per, news, script=None):
    days, w = (1, 'today') if per == 'd' else (7, 'this week')
    M = sorted([(s, chg(s['rows'], days)) for s in stocks if chg(s['rows'], days) is not None], key=lambda x: -x[1])
    if not M: return []
    up = sum(1 for _, p in M if p >= .5); dn = sum(1 for _, p in M if p <= -.5)
    big = [s['n'] for s, p in M if abs(p) >= (3 if per == 'd' else 6)]
    top, tp = max(M, key=lambda x: abs(x[1]))
    date = now.strftime('%A ') + str(now.day) + now.strftime(' %B')
    title = 'Daily closing summary' if per == 'd' else 'Weekly summary'
    if script:
        out = [{'k': 'p', 'h': 'Daily closing wrap' if per == 'd' else 'Weekly stock market wrap', 's': date, 'say': script['intro']}]
        for sg in script['segments']:
            for j, par in enumerate(sg['paragraphs']): out.append({'k': 'c' if j == 0 else 'n', 't': sg['t'], 'title': sg['title'], 'say': par})
        if script['overview']: out.append({'k': 'h', 'say': script['overview']})
        if script['watch']: out.append({'k': 'p', 'h': 'What to watch', 's': '', 'say': script['watch']})
        if script['outro']: out.append({'k': 'p', 'h': 'That is all for now', 's': '', 'say': script['outro']})
        return out
    isw = lambda n: 'was' if n == 1 else 'were'
    return [
        {'k': 'p', 'h': title, 's': date, 'say': f"Good evening. Here is your {'daily closing' if per == 'd' else 'weekly'} stock update for {date}."},
        {'k': 'c', 't': top['t'], 'say': f"{top['n']} had the biggest move. Its share price was {'up' if tp >= 0 else 'down'} {abs(tp):.1f} percent {w}."},
        *[{'k': 'n', 't': it['t'], 'title': it['headline'] or ('Why ' + it['n'] + ' moved'), 'say': news_say(it, i == 0 and it['t'] == top['t'])} for i, it in enumerate(news)],
        {'k': 'h', 'say': f"Overall, {up} of your stocks {isw(up)} up and {dn} {isw(dn)} down {w}."},
        {'k': 'p', 'h': 'That is all for now', 's': '', 'say': ('Keep an eye on ' + ' and '.join(big) + ', which moved a lot.') if big else 'Nothing stood out.'},
    ]


# ---------------- news: find headlines, ask a model WHY, then check its answer ----------------
# Trusted = mainstream outlets and established ASX specialists. Other sites can still be used, but the result is labelled "unverified".
TRUSTED = {'reuters.com', 'afr.com', 'abc.net.au', 'smh.com.au', 'theage.com.au', 'theaustralian.com.au', 'bloomberg.com', 'news.com.au',
           'theguardian.com', 'ft.com', 'wsj.com', 'cnbc.com', 'nine.com.au', 'skynews.com.au', 'thewest.com.au', 'perthnow.com.au', 'couriermail.com.au',
           'marketindex.com.au', 'livewire.com.au', 'fool.com.au', 'stockhead.com.au', 'finnewsnetwork.com.au', 'investsmart.com.au', 'sharecafe.com.au',
           'proactiveinvestors.com.au', 'themarketherald.com.au', 'mining.com.au', 'miningnews.net', 'miningweekly.com', 'mining.com', 'morningstar.com.au',
           'businessnews.com.au', 'thebull.com.au', 'investing.com', 'au.finance.yahoo.com', 'finance.yahoo.com', 'energynewsbulletin.net', 'smartcompany.com.au'}
UA = {'User-Agent': 'Mozilla/5.0 (personal briefing app)'}
LEGAL = re.compile(r'\b(ltd|limited|group|holdings?|corporation|corp|inc|plc|pty)\b\.?', re.I)

def short_name(n):
    s = re.sub(r'\s+', ' ', LEGAL.sub('', n)).strip(' ,.')
    return s or n

def host(u):
    h = urllib.parse.urlparse(u).netloc.lower()
    return h[4:] if h.startswith('www.') else h

def parse_date(s):
    try: return email.utils.parsedate_to_datetime(s).astimezone(SYD).date().isoformat()
    except Exception: return ''

def rss(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r: return ET.fromstring(r.read())

def google(name, ticker, after):
    q = urllib.parse.urlencode({'q': f'("{name}" OR "ASX:{ticker}") after:{after}', 'hl': 'en-AU', 'gl': 'AU', 'ceid': 'AU:en'})
    out = []
    for it in rss('https://news.google.com/rss/search?' + q).iter('item'):
        src = it.find('source'); sname = (src.text or '').strip() if src is not None else ''; dom = host(src.get('url', '')) if src is not None else ''
        title = (it.findtext('title') or '').strip()
        if sname and title.endswith(' - ' + sname): title = title[:-len(sname) - 3].strip()
        out.append({'title': clean_title(title, sname), 'source': sname or dom, 'domain': dom, 'url': it.findtext('link') or '', 'date': parse_date(it.findtext('pubDate'))})
    return out

def bing(name, ticker, after):
    q = urllib.parse.urlencode({'q': f'"{name}" ASX shares', 'format': 'rss', 'setlang': 'en-AU', 'cc': 'AU'})
    out = []
    for it in rss('https://www.bing.com/news/search?' + q).iter('item'):
        link = it.findtext('link') or ''; real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get('url', [link])[0]
        sname = next(((e.text or '').strip() for e in it if e.tag.split('}')[-1] == 'Source'), '')
        out.append({'title': clean_title((it.findtext('title') or '').strip(), sname), 'source': sname or host(real), 'domain': host(real), 'url': real, 'date': parse_date(it.findtext('pubDate'))})
    return out

NAMES = {'afr.com': 'AFR', 'thebull.com.au': 'The Bull', 'kalkinemedia.com': 'Kalkine', 'au.kalkinemedia.com': 'Kalkine', 'fool.com.au': 'Motley Fool Australia',
         'marketindex.com.au': 'Market Index', 'stockhead.com.au': 'Stockhead', 'finnewsnetwork.com.au': 'Finance News Network', 'abc.net.au': 'ABC News',
         'smh.com.au': 'Sydney Morning Herald', 'theage.com.au': 'The Age', 'theaustralian.com.au': 'The Australian', 'reuters.com': 'Reuters', 'bloomberg.com': 'Bloomberg',
         'livewire.com.au': 'Livewire', 'investsmart.com.au': 'InvestSmart', 'sharecafe.com.au': 'Sharecafe', 'proactiveinvestors.com.au': 'Proactive Investors',
         'themarketherald.com.au': 'The Market Herald', 'mining.com.au': 'Mining.com.au', 'news.com.au': 'news.com.au', 'thewest.com.au': 'The West Australian'}

def clean_title(t, source=''):
    """Drop a trailing ' - The Bull' style publisher name from a headline."""
    t = re.sub(r'\s+', ' ', t or '').strip(); known = {v.lower() for v in NAMES.values()} | ({source.lower()} if source else set())
    for sep in (' - ', ' | ', ' – ', ' — '):
        head, _, tail = t.rpartition(sep)
        if head and tail.strip().lower() in known: t = head.strip()
    return t

def tavily_search(query, days, depth='advanced', n=8):
    """News search built for AI: returns titles AND article text. Advanced = 2 credits, basic = 1."""
    body = json.dumps({'api_key': TAVILY_KEY, 'query': query, 'topic': 'news', 'days': days, 'max_results': n, 'search_depth': depth, 'include_answer': 'advanced' if depth == 'advanced' else False}).encode()
    req = urllib.request.Request('https://api.tavily.com/search', data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TAVILY_KEY})
    with urllib.request.urlopen(req, timeout=90) as r: data = json.load(r)
    out = []
    for x in data.get('results', []):
        dom = host(x.get('url', '')); d = (x.get('published_date') or ''); src = NAMES.get(dom, dom)
        try: date = email.utils.parsedate_to_datetime(d).astimezone(SYD).date().isoformat()
        except Exception: date = d[:10] if re.match(r'\d{4}-\d{2}-\d{2}', d) else ''
        out.append({'title': clean_title(x.get('title') or '', src), 'source': src, 'domain': dom, 'url': x.get('url', ''), 'date': date, 'content': re.sub(r'\s+', ' ', x.get('content') or '')[:1200]})
    return out, (data.get('answer') or '').strip()

ANS = {}

def days_since(after): return max(2, (dt.date.today() - dt.date.fromisoformat(after)).days + 1)

def tidy(items, after):
    seen, out = set(), []
    for h in items:
        key = re.sub(r'\W+', ' ', h['title'].lower())[:80]
        if not h['title'] or key in seen or (h['date'] and h['date'] < after): continue
        seen.add(key); h['trusted'] = h['domain'] in TRUSTED; out.append(h)
    out.sort(key=lambda h: h['date'] or '', reverse=True); out.sort(key=lambda h: not h['trusted'])
    return out

def gather(name, ticker, after, p=0.0):
    """First round of evidence: article text from Tavily (if you added a key), plus Google News and Bing News headlines."""
    if NEWS_FAKE:
        fk = [dict(h, trusted=h['domain'] in TRUSTED) for h in FAKE_HEADS.get(ticker, [])]; return fk, {'fake': len(fk)}
    items, counts = [], {}
    feeds = ([('tavily', lambda: tavily_search(f"Why did {name} (ASX:{ticker}) shares {'rise' if p >= 0 else 'fall'}? broker, announcement or news", days_since(after)))] if TAVILY_KEY else []) \
        + [('google', lambda: google(name, ticker, after)), ('bing', lambda: bing(name, ticker, after))]
    for label, fn in feeds:
        try:
            got = fn()
            if label == 'tavily': got, ANS[(ticker, after)] = got
        except Exception as e: print(f'{ticker}: {label} failed', repr(e)); got = []
        counts[label] = len(got); items += got
    if not TAVILY_KEY: counts['tavily'] = 'no key'
    return tidy(items, after)[:14], counts

SYSTEM = ("You are the researcher for a news presenter. A private investor owns shares in an ASX company whose price moved. Read the numbered news items (headlines and extracts) "
          "and write a research note on what the news is saying about the company and why the price probably moved.\n"
          "Rules:\n- Use ONLY facts in the items. No outside knowledge. Never invent figures, names, dates, ratings or quotes.\n"
          "- Plain English. You MAY make a sensible inference about the likely driver when the items point one way, using hedged words such as 'appears', 'likely' or 'reports suggest'.\n"
          "- If the items do not explain the move, say plainly what the news is mostly about and that it does not clearly explain the move. Do not refuse.\n"
          "- Only use numbers that appear in the items or in the price move.\n"
          "Reply with JSON: {\"summary\": 2 to 4 plain sentences under 90 words on what the news says and why the price probably moved, "
          "\"developments\": up to 6 short factual sentences from the items (what happened, who, when), "
          "\"analyst_views\": up to 4 short sentences naming the broker or analyst and their rating or view, only if stated in the items, "
          "\"outlook\": one or two sentences on what investors are watching next, only if the items support it, "
          "\"driver\": one of \"broker\",\"results\",\"guidance\",\"commodity\",\"market\",\"deal\",\"legal\",\"other\",\"unclear\", "
          "\"certainty\": \"high\" or \"medium\" or \"low\", \"support\": [numbers of the items you used], \"headline\": a title of at most 8 words, "
          "\"follow_up_queries\": up to 2 short web search queries that would help confirm the cause, or [] if you are already confident}")

STYLE = ("Good evening. It's Tuesday, the 13th of October, and welcome to your weekly stock market wrap.\n"
         "We begin with the positive performers, and CSL Limited has delivered a welcome result, climbing 4.4 per cent over the past week.\n"
         "Investor confidence has also been tested by analyst downgrades. Barrenjoey lowered its rating in late September, while Jefferies downgraded the company from Buy to Hold this week.\n"
         "The outlook, however, remains uncertain. Investors will be looking for clear evidence that the company can deliver on its production targets while keeping costs under control.")

WRITER = ("You write the script for a professional television-style stock market bulletin, read aloud by a presenter to one private investor. "
          "You are given research notes for the biggest movers on their watchlist. Write the bulletin in the voice of a calm, authoritative business news presenter. "
          "Style example of the tone (do not copy its facts):\n" + STYLE + "\n"
          "Rules:\n- Use ONLY the facts, names, ratings, dates and numbers in the research notes. Never invent anything. Keep every figure exactly as given.\n"
          "- Australian spelling, and say 'per cent' not '%'. Spoken language only: no lists, no headings, no brackets.\n"
          "- Order: gainers first, then fallers, biggest move first. Use natural transitions such as 'We begin with the positive performers', 'Turning now to the stocks under pressure', 'Also finishing in negative territory'.\n"
          "- For each stock write the paragraphs the notes support: the price move, the background and recent developments, analyst views (only if given), and a measured outlook sentence. "
          "If certainty is low or the cause is unclear, say so plainly rather than guessing. Report analyst ratings as the analysts' views, never as your own advice.\n"
          "- Do not tell the investor to buy or sell. Do not say 'according to headlines'. Mention a publication only when it adds credibility.\n"
          "- overview: one short paragraph comparing all the stocks you were given and also the 'others' moves briefly. watch: one paragraph on what to watch for each covered stock. outro: one or two sentences to close.\n"
          "Reply with JSON: {\"intro\": string, \"segments\": [{\"t\": ticker, \"title\": at most 8 words, \"paragraphs\": [string, ...]}], \"overview\": string, \"watch\": string, \"outro\": string}")

CHECKER = ("You are a strict fact-checker for a news script. Below are numbered script paragraphs and the source material they were written from. "
           "List every sentence that states a fact (a number, a name, a date, an analyst rating or an event) that is NOT supported by the source material. "
           "Reasonable hedged inferences such as 'appears' or 'likely' are fine. Reply with JSON: {\"unsupported\": [{\"paragraph\": number, \"sentence\": the exact sentence}]}. "
           "If everything is supported, reply {\"unsupported\": []}.")

FALLBACK_MODELS = ['openai/gpt-4.1-mini', 'openai/gpt-4o-mini', 'openai/gpt-4o']
LLM_ERR = ''

def gemini(system, user, max_tokens, strong=False):
    """Google Gemini (free key from aistudio.google.com). Used first when a GEMINI_KEY secret exists."""
    global LLM_ERR
    import time
    for m in (['gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.0-flash'] if strong else ['gemini-2.5-flash', 'gemini-2.0-flash']):
        for attempt in range(3):
            body = json.dumps({'systemInstruction': {'parts': [{'text': system}]}, 'contents': [{'role': 'user', 'parts': [{'text': user}]}],
                               'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 8192, 'responseMimeType': 'application/json'}}).encode()
            req = urllib.request.Request(f'https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent', data=body, headers={'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_KEY})
            try:
                with urllib.request.urlopen(req, timeout=120) as r: data = json.load(r)
                text = data['candidates'][0]['content']['parts'][0]['text']; time.sleep(2)
                text = re.sub(r'^```(?:json)?|```$', '', text.strip()).strip()
                return json.loads(text[text.index('{'): text.rindex('}') + 1])
            except urllib.error.HTTPError as e:
                detail = ''
                try: detail = e.read().decode('utf-8', 'replace')[:160].replace('\n', ' ')
                except Exception: pass
                LLM_ERR = f'gemini {m}: HTTP {e.code} {detail}'.strip(); print('model call failed:', LLM_ERR)
                if e.code == 429: time.sleep(min(int(e.headers.get('Retry-After') or 20 * (attempt + 1)), 60)); continue
                if e.code in (400, 404): break
                if e.code in (401, 403): return None
                time.sleep(5)
            except Exception as e:
                LLM_ERR = f'gemini {m}: {e!r}'[:200]; print('model call failed:', LLM_ERR); time.sleep(5 * (attempt + 1))
    return None

def chat(system, user, model, max_tokens=2200):
    """One call to GitHub Models. Paces itself, respects rate limits, falls back to other models, and remembers the last error."""
    global LLM_ERR
    if NEWS_FAKE: return FAKE_CHAT(system, user)
    import time
    if GEMINI_KEY:
        got = gemini(system, user, max_tokens, strong=(model == WRITER_MODEL))
        if got is not None: return got
    for m in [model] + [x for x in FALLBACK_MODELS if x != model]:
        for attempt in range(3):
            body = json.dumps({'model': m, 'temperature': 0.2, 'max_tokens': max_tokens, 'response_format': {'type': 'json_object'},
                               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}).encode()
            req = urllib.request.Request('https://models.github.ai/inference/chat/completions', data=body, headers={
                'Authorization': 'Bearer ' + GH_TOKEN, 'Content-Type': 'application/json', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
            try:
                with urllib.request.urlopen(req, timeout=120) as r: text = json.load(r)['choices'][0]['message']['content']
                time.sleep(4)                                               # stay under the free per-minute limit
                text = re.sub(r'^```(?:json)?|```$', '', text.strip()).strip()
                return json.loads(text[text.index('{'): text.rindex('}') + 1])
            except urllib.error.HTTPError as e:
                detail = ''
                try: detail = e.read().decode('utf-8', 'replace')[:160].replace('\n', ' ')
                except Exception: pass
                LLM_ERR = f'{m}: HTTP {e.code} {detail}'.strip(); print('model call failed:', LLM_ERR)
                if e.code == 429: time.sleep(min(int(e.headers.get('Retry-After') or 25 * (attempt + 1)), 90)); continue
                if e.code in (400, 404, 422): break                         # this model or option is not accepted: try the next model
                if e.code in (401, 403): return None                        # not allowed: no point trying others
                time.sleep(5)
            except Exception as e:
                LLM_ERR = f'{m}: {e!r}'[:200]; print('model call failed:', LLM_ERR); time.sleep(5 * (attempt + 1))
    return None

def ask_llm(user): return chat(SYSTEM, user, MODEL)

nums = lambda s: set(re.findall(r'\d+(?:\.\d+)?', s))
NEWS_CACHE = {}

def numbers_ok(text, evidence, move_pct):
    allowed = nums(evidence)
    for n in nums(text):
        v = float(n)
        if n in allowed or abs(v - abs(move_pct)) <= 0.6 or v == round(abs(move_pct)): continue
        return False
    return True

def keep_supported(summary, evidence, move_pct):
    kept = [s for s in re.split(r'(?<=[.!?])\s+', summary.strip()) if s and numbers_ok(s, evidence, move_pct)]
    return ' '.join(kept)

def prompt(s, move, date, heads, final):
    lines = '\n'.join(f"{i}. [{h['source']}, {h['date']}] {h['title']}" + (f" - {h['content']}" if h.get('content') else '') for i, h in enumerate(heads, 1))[:14000]
    return f"Company: {s['n']} ({s['t']}). Its share price {move}. The move was on {date}.\nNews items:\n{lines}" + ("\nThis is the final pass, so set follow_up_queries to []." if final else '')

def use_extracts(item, s, p, heads):
    """If the AI writing step is unavailable, report what the articles say (Tavily's own summary, else the opening sentences of the articles) instead of headlines."""
    evidence = ' '.join(h['title'] + ' ' + h.get('content', '') for h in heads)
    text = keep_supported(ANS.get((s['t'], item.get('after', '')), ''), evidence, p)
    if not text:
        key = {short_name(s['n']).lower().split()[0], s['t'].lower()}; out = []
        for h in heads[:4]:
            for sent in re.split(r'(?<=[.!?])\s+', h.get('content', '')):
                if len(sent) > 50 and any(k in sent.lower() for k in key): out.append(sent.strip()); break
        text = keep_supported(' '.join(out[:2]), evidence, p)
    if not text: return item
    top = heads[:3]; trusted = list(dict.fromkeys(h['domain'] for h in top if h.get('trusted')))
    item.update(reason=text, headline=item['n'], certainty='low', via='extracts', why_none='',
                confidence='multiple' if len(trusted) >= 2 else 'single' if len(trusted) == 1 else 'unverified',
                sources=[{'title': h['title'], 'source': h['source'], 'url': h['url'], 'date': h['date']} for h in top])
    return item

def explain(s, p, per):
    """Researches one stock. confidence = how many trusted sources back it (multiple | single | unverified), or none."""
    w = 'today' if per == 'd' else 'this week'
    move = f"{'rose' if p >= 0 else 'fell'} {abs(p):.1f} percent {w}"; date = s['rows'][-1][0]
    item = {'t': s['t'], 'n': s['n'], 'move': move, 'pct': round(p, 1), 'reason': None, 'headline': None, 'confidence': 'none', 'certainty': '', 'driver': '', 'developments': [], 'analyst_views': [], 'outlook': '',
            'sources': [], 'why_none': '', 'why_detail': '', 'found': 0, 'headlines': [], 'counts': {}, 'evidence': '', 'after': '', 'via': ''}
    after = (dt.date.fromisoformat(date) - dt.timedelta(days=1 if per == 'd' else 8)).isoformat()   # anchored to the day of the move
    name = short_name(s['n']); item['after'] = after
    try: heads, counts = NEWS_CACHE.setdefault((s['t'], after, per), gather(name, s['t'], after, p))
    except Exception as e:
        print('news fetch failed for', s['t'], repr(e)); item['why_none'] = 'fetch_failed'; return item
    print(f"{s['t']}: searched '{name}' since {after}: {counts}, kept {len(heads)} items ({sum(1 for h in heads if h.get('trusted'))} from trusted sites)")
    item['found'] = len(heads); item['counts'] = dict(counts); item['headlines'] = [{k: h[k] for k in ('title', 'source', 'url', 'date')} for h in heads[:3]]
    if not heads: item['why_none'] = 'no_headlines'; return item
    ans = ask_llm(prompt(s, move, date, heads, False))
    if not isinstance(ans, dict) or not str(ans.get('summary') or '').strip():
        item['why_none'] = 'model_unavailable' if not isinstance(ans, dict) else 'model_no_reason'; item['why_detail'] = LLM_ERR if not isinstance(ans, dict) else ''
        return use_extracts(item, s, p, heads)
    qs = [q.strip()[:120] for q in (ans.get('follow_up_queries') or []) if isinstance(q, str) and q.strip()][:2]
    if qs and ans.get('certainty') != 'high' and TAVILY_KEY and not NEWS_FAKE:      # a second round of research when the first was not conclusive
        more = []
        for q in qs:
            try: more += tavily_search(q if name.lower() in q.lower() else f"{name} {q}", days_since(after), 'basic', 5)[0]
            except Exception as e: print(f"{s['t']}: follow-up search failed", repr(e))
        item['counts']['follow_ups'] = len(qs)
        heads = tidy(heads + more, after)[:14]; item['found'] = len(heads)
        ans2 = ask_llm(prompt(s, move, date, heads, True))
        if isinstance(ans2, dict) and str(ans2.get('summary') or '').strip(): ans = ans2
    sup = [i for i in (ans.get('support') or []) if isinstance(i, int) and 1 <= i <= len(heads)]
    chosen = [heads[i - 1] for i in dict.fromkeys(sup)] or heads[:3]
    evidence = ' '.join(h['title'] + ' ' + h.get('content', '') for h in heads)
    def clean(x): return [str(v).strip() for v in (x or []) if isinstance(v, str) and v.strip() and numbers_ok(v, evidence, p)][:6]
    summary = str(ans['summary']).strip()
    if not numbers_ok(summary, evidence, p):          # a figure that is not in the news items: ask for a rewrite without it
        fix = ask_llm(prompt(s, move, date, heads, True) + f"\nYour earlier summary used figures that are not in the news items: {summary}\nRewrite the summary without those figures. Describe them in words instead of numbers.")
        fixed = str(fix.get('summary') or '').strip() if isinstance(fix, dict) else ''
        first = re.split(r'(?<=[.!?])\s+', summary)[0]
        summary = fixed if fixed and numbers_ok(fixed, evidence, p) else (keep_supported(summary, evidence, p) if numbers_ok(first, evidence, p) else '')
    if not summary: item['why_none'] = 'numbers_dropped'; return use_extracts(item, s, p, heads)
    trusted = list(dict.fromkeys(h['domain'] for h in chosen if h.get('trusted')))
    item.update(reason=summary, headline=str(ans.get('headline') or s['n'])[:80], certainty=str(ans.get('certainty') or 'medium'), driver=str(ans.get('driver') or ''),
                developments=clean(ans.get('developments')), analyst_views=clean(ans.get('analyst_views')), outlook=(clean([ans.get('outlook')]) or [''])[0],
                confidence='multiple' if len(trusted) >= 2 else 'single' if len(trusted) == 1 else 'unverified',
                sources=[{'title': h['title'], 'source': h['source'], 'url': h['url'], 'date': h['date']} for h in chosen[:4]],
                evidence=' '.join(f"{h['source']}: {h['title']}. {h.get('content', '')[:350]}" for h in heads[:10]))
    return item

def join_names(xs): return xs[0] if len(xs) == 1 else ', '.join(xs[:-1]) + ' and ' + xs[-1]

def news_say(item, first):
    """Simple fallback wording, used only if the script writer is unavailable."""
    pre = '' if first else f"{item['n']} {item['move']}. "
    if item['confidence'] == 'none':
        if item['headlines']: h = item['headlines'][0]; return pre + f"Headlines about {item['n']} include: {h['title']}, from {h['source']}."
        return pre + f"I found no recent news about {item['n']}, so I cannot say why it moved."
    names = list(dict.fromkeys(x['source'] for x in item['sources']))[:2]
    tail = {'multiple': f" Several sources report this, including {join_names(names)}.",
            'single': f" This comes from one source, {names[0]}, so treat it with care.",
            'unverified': f" This comes only from lesser-known sources such as {names[0]}, so it is unconfirmed."}[item['confidence']]
    return pre + item['reason'] + tail

def research(stocks, per):
    days = 1 if per == 'd' else 7
    M = sorted([(s, chg(s['rows'], days)) for s in stocks if chg(s['rows'], days) is not None], key=lambda x: -abs(x[1]))[:int(os.environ.get('NEWS_TOP', '3'))]
    return [explain(s, p, per) for s, p in M]

def ordinal(n): return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"

def write_script(stocks, per, items):
    """Stage 2 and 3: a professional presenter script from the research notes, then a fact-check pass. Returns a dict or None."""
    got = [it for it in items if it['confidence'] != 'none']
    if not got: return None
    days = 1 if per == 'd' else 7
    date = f"{now.strftime('%A')}, the {ordinal(now.day)} of {now.strftime('%B')}"
    notes = [{'t': it['t'], 'company': it['n'], 'move': it['move'], 'percent_move': it['pct'], 'certainty': it['certainty'], 'summary': it['reason'], 'developments': it['developments'],
              'analyst_views': it['analyst_views'], 'outlook': it['outlook']} for it in sorted(got, key=lambda x: -x['pct'])]
    done = {it['t'] for it in got}
    others = [{'company': s['n'], 'percent_move': round(chg(s['rows'], days), 1)} for s in stocks if s['t'] not in done and chg(s['rows'], days) is not None]
    payload = json.dumps({'date': date, 'bulletin': 'daily closing wrap' if per == 'd' else 'weekly stock market wrap', 'period': 'today' if per == 'd' else 'over the past week',
                          'target_words': 330 if per == 'd' else 520, 'stocks': notes, 'others': others})
    facts = payload + ' ' + ' '.join(it['evidence'] for it in got)
    sc = chat(WRITER, payload, WRITER_MODEL, 3200)
    def paras(x): return [x.get('intro', '')] + [q for sg in x.get('segments', []) for q in sg.get('paragraphs', [])] + [x.get('overview', ''), x.get('watch', ''), x.get('outro', '')]
    def bad(x): return sorted({n for q in paras(x) if isinstance(q, str) for n in nums(q) if not numbers_ok(n, facts, 0.0)})
    if not isinstance(sc, dict) or not sc.get('segments'): print('script writer unavailable:', LLM_ERR); return None
    b = bad(sc)
    if b:
        print('script used figures not in the notes, asking for a rewrite:', b)
        sc2 = chat(WRITER, payload + f"\nYour earlier script used figures that are not in the notes: {', '.join(b)}. Rewrite the whole script without them.", WRITER_MODEL, 3200)
        if isinstance(sc2, dict) and sc2.get('segments') and not bad(sc2): sc = sc2
    flat = [(i, q) for i, q in enumerate(paras(sc)) if isinstance(q, str) and q.strip()]
    chk = chat(CHECKER, 'Source material:\n' + facts[:12000] + '\n\nScript paragraphs:\n' + '\n'.join(f'{i}. {q}' for i, q in flat), MODEL, 1200)
    drop = {}
    if isinstance(chk, dict):
        for u in chk.get('unsupported', []) or []:
            if isinstance(u, dict) and isinstance(u.get('paragraph'), int) and isinstance(u.get('sentence'), str): drop.setdefault(u['paragraph'], []).append(u['sentence'].strip())
        if drop: print('fact-check removed sentences:', drop)
    def fix(i, q):
        for sent in drop.get(i, []): q = q.replace(sent, '')
        return re.sub(r'\s+', ' ', q).strip()
    k = [0]
    def nxt(q):
        i = k[0]; k[0] += 1; return fix(i, q) if isinstance(q, str) else ''
    out = {'intro': nxt(sc.get('intro', '')), 'segments': []}
    for sg in sc.get('segments', []):
        ps = [t for t in (nxt(q) for q in sg.get('paragraphs', [])) if t]
        if ps: out['segments'].append({'t': sg.get('t'), 'title': str(sg.get('title') or '')[:80], 'paragraphs': ps})
    out['overview'], out['watch'], out['outro'] = nxt(sc.get('overview', '')), nxt(sc.get('watch', '')), nxt(sc.get('outro', ''))
    return out if out['intro'] and out['segments'] else None

# test fixtures, used only when NEWS_FAKE=1
FAKE_HEADS = {
    'BHP': [{'title': 'BHP shares slide as China curbs iron ore imports', 'source': 'Reuters', 'domain': 'reuters.com', 'url': 'https://www.reuters.com/x', 'date': '2026-10-09', 'content': 'Jefferies cut BHP to Hold.'},
            {'title': 'Iron ore falls after China steps up port checks', 'source': 'AFR', 'domain': 'afr.com', 'url': 'https://www.afr.com/y', 'date': '2026-10-09'}],
    'CBA': [{'title': 'Commonwealth Bank lifts home loan rates', 'source': 'ABC News', 'domain': 'abc.net.au', 'url': 'https://www.abc.net.au/z', 'date': '2026-10-09'}],
    'CSL': [{'title': 'CSL cuts outlook', 'source': 'AFR', 'domain': 'afr.com', 'url': 'https://www.afr.com/w', 'date': '2026-10-09'}],
    'TLS': [{'title': 'Telstra to raise mobile prices from December', 'source': 'Some Blog', 'domain': 'someblog.example', 'url': 'https://someblog.example/t', 'date': '2026-10-09'}],
    'WOW': [{'title': 'Woolworths opens new store in Perth', 'source': 'Perth Now', 'domain': 'perthnow.com.au', 'url': 'https://www.perthnow.com.au/w', 'date': '2026-10-09'}]}
def FAKE_CHAT(system, user):
    if 'strict fact-checker' in system:
        return {'unsupported': [{'paragraph': int(l.split('.')[0]), 'sentence': 'The chief executive also resigned, which is FABRICATED.'} for l in user.split('\n') if 'FABRICATED' in l]}
    if 'stock market bulletin' in system:
        ts = [x['t'] for x in json.loads(user.split('\n')[0])['stocks']]
        return {'intro': 'Good evening. Welcome to your stock market wrap.', 'segments': [{'t': t, 'title': t + ' in focus', 'paragraphs': [f'{t} was a major mover this period.', 'The background is set out in the news. The chief executive also resigned, which is FABRICATED.', 'The outlook remains uncertain.']} for t in ts],
                'overview': 'That is how the covered stocks compare.', 'watch': 'We will be watching each of them closely.', 'outro': 'That is where things stand.'}
    if 'BHP' in user: return {'summary': 'BHP shares fell as China moved to restrict iron ore imports, which also pushed the iron ore price down.', 'developments': ['China stepped up port checks on iron ore.'], 'analyst_views': ['Jefferies cut BHP to Hold.'], 'outlook': 'Investors are watching Chinese demand.', 'driver': 'commodity', 'certainty': 'high', 'support': [1, 2], 'headline': 'China curbs hit BHP', 'follow_up_queries': []}
    if 'CBA' in user: return {'summary': 'Commonwealth Bank raised its home loan rates. That appears to be the main news around the bank.', 'developments': [], 'analyst_views': [], 'outlook': '', 'driver': 'other', 'certainty': 'medium', 'support': [1], 'headline': 'CBA lifts loan rates', 'follow_up_queries': []}
    if 'CSL' in user and 'Rewrite the summary without' in user: return {'summary': 'CSL cut its outlook, which analysts are likely to focus on.', 'driver': 'guidance', 'certainty': 'medium', 'support': [1], 'headline': 'CSL cuts outlook', 'follow_up_queries': []}
    if 'CSL' in user: return {'summary': 'CSL cut its outlook by 40 percent. Analysts are likely to focus on that.', 'driver': 'guidance', 'certainty': 'medium', 'support': [1], 'headline': 'CSL cuts outlook', 'follow_up_queries': []}
    if 'TLS' in user: return {'summary': 'Telstra shares moved after reports it will raise mobile prices.', 'driver': 'other', 'certainty': 'low', 'support': [1], 'headline': 'Telstra price rise', 'follow_up_queries': []}
    if 'WOW' in user: return {'summary': 'Woolworths is mostly in the news for opening a new Perth store. None of it clearly explains the price move.', 'driver': 'unclear', 'certainty': 'low', 'support': [1], 'headline': 'Woolworths Perth store', 'follow_up_queries': []}
    return None

def make_tts():
    if TTS == 'off': return None
    os.makedirs(os.path.join(SITE, 'data', 'audio'), exist_ok=True)
    if TTS == 'fake':
        def say(text, base):
            with wave.open(base + '.wav', 'wb') as f: f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000); f.writeframes(b'\x00\x00' * 4000)
            return base + '.wav'
        return say
    try:
        import numpy as np, soundfile as sf
        from kokoro import KPipeline
        pipe = KPipeline(lang_code='b' if VOICE.startswith('b') else 'a')
        def say(text, base):
            parts = [a.numpy() if hasattr(a, 'numpy') else np.asarray(a) for _, _, a in pipe(spoken(text), voice=VOICE)]
            sf.write(base + '.wav', np.concatenate(parts), 24000)
            if shutil.which('ffmpeg'):
                subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', base + '.wav', '-b:a', '64k', base + '.mp3'], check=True); os.remove(base + '.wav'); return base + '.mp3'
            return base + '.wav'
        return say
    except Exception as e:
        print('Voice engine unavailable, the page will use its own voice:', repr(e)); return None

def asx_list():
    if FAKE: return [['BHP', 'BHP Group', 'Common Stock'], ['TST', 'Test Mining Ltd', 'Common Stock'], ['TSE', 'Test ETF', 'ETF'], ['TLS', 'Telstra Group Ltd', 'Common Stock']]
    q = urllib.parse.urlencode({'fmt': 'json', 'api_token': KEY})
    with urllib.request.urlopen(f'https://eodhd.com/api/exchange-symbol-list/AU?{q}', timeout=60) as r:
        data = json.load(r)
    return [[o['Code'], o['Name'], o['Type']] for o in data if o.get('Code') and o.get('Name') and o.get('Type') in ('Common Stock', 'ETF')]

def main():
    stocks, fails = [], []
    for t, n in watchlist():
        try:
            rows = fetch(t)
            if len(rows) < 2: raise ValueError('no price rows returned')
            stocks.append({'t': t, 'n': n, 'rows': rows[-400:]})
        except Exception as e:
            fails.append(f'{t}: {e}'); print('FAILED', t, e)
    if not stocks: sys.exit('No prices could be fetched. The previous version of the site stays live.')
    if os.path.isdir(SITE): shutil.rmtree(SITE)
    os.makedirs(os.path.join(SITE, 'data'))
    shutil.copy(os.path.join(ROOT, 'index.html'), os.path.join(SITE, 'index.html'))
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    json.dump({'updated': stamp, 'stocks': stocks, 'failed': fails}, open(os.path.join(SITE, 'data', 'prices.json'), 'w'))
    if len(stocks) < 20:   # the company list costs 1 EODHD call, so skip it if you use all 20 free calls on prices
        try: json.dump(asx_list(), open(os.path.join(SITE, 'data', 'asx.json'), 'w'), separators=(',', ':'))
        except Exception as e: print('Company list not updated:', repr(e))
    news_out = {}
    say = make_tts(); bul = {'updated': stamp, 'for': max(r[0] for s in stocks for r in s['rows'][-1:])}
    for per, name in (('d', 'daily'), ('w', 'weekly')):
        news = research(stocks, per) if os.environ.get('NEWS', 'on') != 'off' else []
        news_out[name] = news
        script = write_script(stocks, per, news) if news and os.environ.get('NEWS', 'on') != 'off' else None
        sc = scenes(stocks, per, news, script)
        for i, x in enumerate(sc, 1):
            x['audio'] = None
            if say:
                try: x['audio'] = 'data/audio/' + os.path.basename(say(x['say'], os.path.join(SITE, 'data', 'audio', f'{name}-{i}')))
                except Exception as e: print('Audio failed for', name, i, repr(e))
        bul[name] = {'scenes': sc}
    json.dump(bul, open(os.path.join(SITE, 'data', 'bulletin.json'), 'w'))
    json.dump({'updated': stamp, 'llm_error': LLM_ERR, **news_out}, open(os.path.join(SITE, 'data', 'news.json'), 'w'))
    print('Built site with', len(stocks), 'stocks.', ('Some failed: ' + '; '.join(fails)) if fails else '')

main()
