"""Daily job: fetch ASX closing prices (EODHD), write the bulletin script, make the voice audio, build ./site.
Env: EODHD_KEY (secret). Testing only: EODHD_FAKE=1 (made-up prices), TTS=off|fake. VOICE (default bm_george)."""
import os, json, re, sys, shutil, subprocess, datetime as dt, urllib.request, urllib.parse, math, wave, email.utils, xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
SYD = ZoneInfo('Australia/Sydney'); ROOT = os.path.dirname(os.path.abspath(__file__)); SITE = os.path.join(ROOT, 'site')
try: SECRETS = json.loads(os.environ.get('SECRETS_JSON') or '{}')
except Exception: SECRETS = {}
KEY = os.environ.get('EODHD_KEY') or SECRETS.get('EODHD_KEY', ''); FAKE = os.environ.get('EODHD_FAKE') == '1'; TTS = os.environ.get('TTS', 'on'); VOICE = os.environ.get('VOICE', 'bm_george')
NEWS_FAKE = os.environ.get('NEWS_FAKE') == '1'; MODEL = os.environ.get('NEWS_MODEL', 'openai/gpt-4.1'); GH_TOKEN = os.environ.get('GITHUB_TOKEN') or SECRETS.get('github_token', ''); TAVILY_KEY = os.environ.get('TAVILY_KEY') or SECRETS.get('TAVILY_KEY', '')

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

def scenes(stocks, per, news):
    days, w = (1, 'today') if per == 'd' else (7, 'this week')
    M = sorted([(s, chg(s['rows'], days)) for s in stocks if chg(s['rows'], days) is not None], key=lambda x: -x[1])
    if not M: return []
    up = sum(1 for _, p in M if p >= .5); dn = sum(1 for _, p in M if p <= -.5)
    big = [s['n'] for s, p in M if abs(p) >= (3 if per == 'd' else 6)]
    top, tp = max(M, key=lambda x: abs(x[1]))
    date = now.strftime('%A ') + str(now.day) + now.strftime(' %B')
    title = 'Daily closing summary' if per == 'd' else 'Weekly summary'
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
        out.append({'title': title, 'source': sname or dom, 'domain': dom, 'url': it.findtext('link') or '', 'date': parse_date(it.findtext('pubDate'))})
    return out

def bing(name, ticker, after):
    q = urllib.parse.urlencode({'q': f'"{name}" ASX shares', 'format': 'rss', 'setlang': 'en-AU', 'cc': 'AU'})
    out = []
    for it in rss('https://www.bing.com/news/search?' + q).iter('item'):
        link = it.findtext('link') or ''; real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get('url', [link])[0]
        sname = next(((e.text or '').strip() for e in it if e.tag.split('}')[-1] == 'Source'), '')
        out.append({'title': (it.findtext('title') or '').strip(), 'source': sname or host(real), 'domain': host(real), 'url': real, 'date': parse_date(it.findtext('pubDate'))})
    return out

NAMES = {'afr.com': 'AFR', 'thebull.com.au': 'The Bull', 'kalkinemedia.com': 'Kalkine', 'au.kalkinemedia.com': 'Kalkine', 'fool.com.au': 'Motley Fool Australia',
         'marketindex.com.au': 'Market Index', 'stockhead.com.au': 'Stockhead', 'finnewsnetwork.com.au': 'Finance News Network', 'abc.net.au': 'ABC News',
         'smh.com.au': 'Sydney Morning Herald', 'theage.com.au': 'The Age', 'theaustralian.com.au': 'The Australian', 'reuters.com': 'Reuters', 'bloomberg.com': 'Bloomberg',
         'livewire.com.au': 'Livewire', 'investsmart.com.au': 'InvestSmart', 'sharecafe.com.au': 'Sharecafe', 'proactiveinvestors.com.au': 'Proactive Investors',
         'themarketherald.com.au': 'The Market Herald', 'mining.com.au': 'Mining.com.au', 'news.com.au': 'news.com.au', 'thewest.com.au': 'The West Australian'}

def tavily(name, ticker, p, after):
    """News search built for AI: returns titles AND article text. Needs a free TAVILY_KEY."""
    days = max(2, (dt.date.today() - dt.date.fromisoformat(after)).days + 1)
    body = json.dumps({'api_key': TAVILY_KEY, 'query': f"Why did {name} (ASX:{ticker}) shares {'rise' if p >= 0 else 'fall'}? broker, announcement or news",
                       'topic': 'news', 'days': days, 'max_results': 10, 'search_depth': 'basic'}).encode()
    req = urllib.request.Request('https://api.tavily.com/search', data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TAVILY_KEY})
    with urllib.request.urlopen(req, timeout=60) as r: data = json.load(r)
    out = []
    for x in data.get('results', []):
        dom = host(x.get('url', '')); d = (x.get('published_date') or '')
        try: date = email.utils.parsedate_to_datetime(d).astimezone(SYD).date().isoformat()
        except Exception: date = d[:10] if re.match(r'\d{4}-\d{2}-\d{2}', d) else ''
        out.append({'title': (x.get('title') or '').strip(), 'source': NAMES.get(dom, dom), 'domain': dom, 'url': x.get('url', ''), 'date': date, 'content': re.sub(r'\s+', ' ', x.get('content') or '')[:600]})
    return out

def gather(name, ticker, after, p=0.0):
    """Evidence for a move: article text from Tavily (if you added a key), plus Google News and Bing News headlines."""
    if NEWS_FAKE:
        fk = [dict(h, trusted=h['domain'] in TRUSTED) for h in FAKE_HEADS.get(ticker, [])]; return fk, {'fake': len(fk)}
    items, counts = [], {}
    feeds = ([('tavily', lambda: tavily(name, ticker, p, after))] if TAVILY_KEY else []) + [('google', lambda: google(name, ticker, after)), ('bing', lambda: bing(name, ticker, after))]
    for label, fn in feeds:
        try: got = fn()
        except Exception as e: print(f'{ticker}: {label} failed', repr(e)); got = []
        counts[label] = len(got); items += got
    if not TAVILY_KEY: counts['tavily'] = 'no key'
    seen, out = set(), []
    for h in items:
        key = re.sub(r'\W+', ' ', h['title'].lower())[:80]
        if not h['title'] or key in seen or (h['date'] and h['date'] < after): continue
        seen.add(key); h['trusted'] = h['domain'] in TRUSTED; out.append(h)
    out.sort(key=lambda h: h['date'] or '', reverse=True); out.sort(key=lambda h: not h['trusted'])
    return out[:15], counts

SYSTEM = ("You explain share price moves to a private investor in plain English. Use ONLY the numbered headlines and article extracts you are given. Do not use outside knowledge. "
          "Reply with JSON: {\"reason\": string or null, \"support\": [headline numbers], \"headline\": string}. "
          "Find the best-supported cause of the move. Prefer headlines dated on or just before the move. If several headlines point to the same development, explain that one. "
          "reason: one or two short sentences (under 45 words), simple words, saying what happened and how it relates to the price move. "
          "Use wording like 'after', 'as' or 'reports say'. Never claim the news definitely caused the move. Do not add numbers that are not in the text you are given. "
          "Only set reason to null if no headline gives a plausible cause for a move in this direction. support lists the headline numbers that back your reason. "
          "headline: a title of at most 8 words.")

def ask_llm(user):
    if NEWS_FAKE: return FAKE_LLM(user)
    body = json.dumps({'model': MODEL, 'temperature': 0, 'response_format': {'type': 'json_object'},
                       'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': user}]}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request('https://models.github.ai/inference/chat/completions', data=body, headers={
                'Authorization': 'Bearer ' + GH_TOKEN, 'Content-Type': 'application/json', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
            with urllib.request.urlopen(req, timeout=90) as r: return json.loads(json.load(r)['choices'][0]['message']['content'])
        except Exception as e:
            print('model call failed', attempt + 1, repr(e)); import time; time.sleep(5 * (attempt + 1))
    return None

nums = lambda s: set(re.findall(r'\d+(?:\.\d+)?', s))
NEWS_CACHE = {}

def numbers_ok(reason, support_text, move_pct):
    allowed = nums(support_text)
    for n in nums(reason):
        v = float(n)
        if n in allowed or abs(v - abs(move_pct)) <= 0.6 or v == round(abs(move_pct)): continue
        return False
    return True

def explain(s, p, per):
    """Returns {t,n,move,reason,headline,confidence,sources,why_none,found,headlines}. confidence: multiple | single | unverified | none."""
    w = 'today' if per == 'd' else 'this week'
    move = f"{'rose' if p >= 0 else 'fell'} {abs(p):.1f} percent {w}"
    item = {'t': s['t'], 'n': s['n'], 'move': move, 'reason': None, 'headline': None, 'confidence': 'none', 'sources': [], 'why_none': '', 'found': 0, 'headlines': []}
    after = (dt.date.fromisoformat(s['rows'][-1][0]) - dt.timedelta(days=1 if per == 'd' else 8)).isoformat()   # anchored to the day of the move
    name = short_name(s['n'])
    try:
        heads, counts = NEWS_CACHE.setdefault((s['t'], after, per), gather(name, s['t'], after, p))
    except Exception as e:
        print('news fetch failed for', s['t'], repr(e)); item['why_none'] = 'fetch_failed'; return item
    print(f"{s['t']}: searched '{name}' since {after}: {counts}, kept {len(heads)} headlines ({sum(1 for h in heads if h.get('trusted'))} from trusted sites)")
    item['found'] = len(heads); item['counts'] = counts; item['headlines'] = [{k: h[k] for k in ('title', 'source', 'url', 'date')} for h in heads[:3]]
    if not heads: item['why_none'] = 'no_headlines'; return item
    lines = '\n'.join(f"{i}. [{h['source']}, {h['date']}] {h['title']}" + (f" - {h['content']}" if h.get('content') else '') for i, h in enumerate(heads, 1))
    ans = ask_llm(f"Company: {s['n']} ({s['t']}). Its share price {move}. The move was on {s['rows'][-1][0]}.\nHeadlines:\n{lines}")
    if not isinstance(ans, dict): item['why_none'] = 'model_unavailable'; return item
    if not ans.get('reason'): item['why_none'] = 'model_no_reason'; return item
    sup = [i for i in ans.get('support', []) if isinstance(i, int) and 1 <= i <= len(heads)]
    if not sup: item['why_none'] = 'no_support'; return item
    chosen = [heads[i - 1] for i in dict.fromkeys(sup)]
    reason = str(ans['reason']).strip()
    if not numbers_ok(reason, ' '.join(h['title'] + ' ' + h.get('content', '') for h in chosen), p):
        print('dropped an explanation with numbers not found in the headlines for', s['t']); item['why_none'] = 'numbers_dropped'; return item
    trusted = list(dict.fromkeys(h['domain'] for h in chosen if h.get('trusted')))
    item.update(reason=reason if reason.endswith('.') else reason + '.', headline=str(ans.get('headline') or s['n'])[:80],
                confidence='multiple' if len(trusted) >= 2 else 'single' if len(trusted) == 1 else 'unverified',
                sources=[{'title': h['title'], 'source': h['source'], 'url': h['url'], 'date': h['date']} for h in chosen])
    return item

def join_names(xs): return xs[0] if len(xs) == 1 else ', '.join(xs[:-1]) + ' and ' + xs[-1]

def news_say(item, first):
    pre = '' if first else f"{item['n']} {item['move']}. "
    if item['confidence'] == 'none':
        if item['headlines']: h = item['headlines'][0]; return pre + f"I could not find a clear reason for {item['n']}'s move. The latest headline I found was: {h['title']}, from {h['source']}."
        return pre + f"I found no recent news about {item['n']}, so I cannot say why it moved."
    names = list(dict.fromkeys(x['source'] for x in item['sources']))[:2]
    tail = {'multiple': f" Several sources report this, including {join_names(names)}.",
            'single': f" Only one source, {names[0]}, reports this, so treat it with care.",
            'unverified': f" This is only reported by lesser-known sources such as {names[0]}, so it is unconfirmed."}[item['confidence']]
    return pre + item['reason'] + tail

def research(stocks, per):
    days = 1 if per == 'd' else 7
    M = sorted([(s, chg(s['rows'], days)) for s in stocks if chg(s['rows'], days) is not None], key=lambda x: -abs(x[1]))[:int(os.environ.get('NEWS_TOP', '3'))]
    return [explain(s, p, per) for s, p in M]

# test fixtures, used only when NEWS_FAKE=1
FAKE_HEADS = {
    'BHP': [{'title': 'BHP shares slide as China curbs iron ore imports', 'source': 'Reuters', 'domain': 'reuters.com', 'url': 'https://www.reuters.com/x', 'date': '2026-10-09'},
            {'title': 'Iron ore falls after China steps up port checks', 'source': 'AFR', 'domain': 'afr.com', 'url': 'https://www.afr.com/y', 'date': '2026-10-09'}],
    'CBA': [{'title': 'Commonwealth Bank lifts home loan rates', 'source': 'ABC News', 'domain': 'abc.net.au', 'url': 'https://www.abc.net.au/z', 'date': '2026-10-09'}],
    'CSL': [{'title': 'CSL cuts outlook by 12 percent', 'source': 'AFR', 'domain': 'afr.com', 'url': 'https://www.afr.com/w', 'date': '2026-10-09'}],
}
FAKE_HEADS['TLS'] = [{'title': 'Telstra to raise mobile prices from December', 'source': 'Some Blog', 'domain': 'someblog.example', 'url': 'https://someblog.example/t', 'date': '2026-10-09'}]
FAKE_HEADS['WOW'] = [{'title': 'Woolworths opens new store in Perth', 'source': 'Perth Now', 'domain': 'perthnow.com.au', 'url': 'https://www.perthnow.com.au/w', 'date': '2026-10-09'}]
def FAKE_LLM(user):
    if 'TLS' in user: return {'reason': 'Telstra shares moved after reports it will raise mobile prices.', 'support': [1], 'headline': 'Telstra price rise'}
    if 'WOW' in user: return {'reason': None, 'support': [], 'headline': ''}
    if 'BHP' in user: return {'reason': 'BHP shares fell as China moved to restrict iron ore imports, which also pushed the iron ore price down.', 'support': [1, 2], 'headline': 'China curbs hit BHP'}
    if 'CBA' in user: return {'reason': 'Commonwealth Bank raised its home loan rates.', 'support': [1], 'headline': 'CBA lifts loan rates'}
    if 'CSL' in user: return {'reason': 'CSL cut its outlook by 40 percent.', 'support': [1], 'headline': 'CSL cuts outlook'}   # 40 is not in the headline: must be dropped
    return {'reason': None, 'support': [], 'headline': ''}

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
        sc = scenes(stocks, per, news)
        for i, x in enumerate(sc, 1):
            x['audio'] = None
            if say:
                try: x['audio'] = 'data/audio/' + os.path.basename(say(x['say'], os.path.join(SITE, 'data', 'audio', f'{name}-{i}')))
                except Exception as e: print('Audio failed for', name, i, repr(e))
        bul[name] = {'scenes': sc}
    json.dump(bul, open(os.path.join(SITE, 'data', 'bulletin.json'), 'w'))
    json.dump({'updated': stamp, **news_out}, open(os.path.join(SITE, 'data', 'news.json'), 'w'))
    print('Built site with', len(stocks), 'stocks.', ('Some failed: ' + '; '.join(fails)) if fails else '')

main()
