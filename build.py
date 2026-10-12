"""Daily job: fetch ASX closing prices (EODHD), write the bulletin script, make the voice audio, build ./site.
Env: EODHD_KEY (secret). Testing only: EODHD_FAKE=1 (made-up prices), TTS=off|fake. VOICE (default bm_george)."""
import os, json, re, sys, shutil, subprocess, datetime as dt, urllib.request, urllib.parse, math, wave, email.utils, xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
SYD = ZoneInfo('Australia/Sydney'); ROOT = os.path.dirname(os.path.abspath(__file__)); SITE = os.path.join(ROOT, 'site')
KEY = os.environ.get('EODHD_KEY', ''); FAKE = os.environ.get('EODHD_FAKE') == '1'; TTS = os.environ.get('TTS', 'on'); VOICE = os.environ.get('VOICE', 'bm_george')
NEWS_FAKE = os.environ.get('NEWS_FAKE') == '1'; MODEL = os.environ.get('NEWS_MODEL', 'openai/gpt-4.1'); GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')

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
# Only these publishers count as evidence. Edit the list to suit you.
TRUSTED = {'reuters.com', 'afr.com', 'abc.net.au', 'smh.com.au', 'theage.com.au', 'theaustralian.com.au', 'bloomberg.com', 'news.com.au',
           'theguardian.com', 'marketindex.com.au', 'livewire.com.au', 'ft.com', 'wsj.com', 'cnbc.com', 'miningweekly.com', 'investing.com',
           'morningstar.com.au', 'businessnews.com.au', 'nine.com.au', 'skynews.com.au', 'smartcompany.com.au', 'mining.com', 'energynewsbulletin.net'}
UA = {'User-Agent': 'Mozilla/5.0 (personal briefing app)'}

def host(u):
    h = urllib.parse.urlparse(u).netloc.lower()
    return h[4:] if h.startswith('www.') else h

def gnews(name, ticker, days):
    """Headlines from Google News RSS (public, unofficial). Returns [{title, source, domain, url, date}]."""
    if NEWS_FAKE: return FAKE_HEADS.get(ticker, [])
    q = urllib.parse.urlencode({'q': f'"{name}" ASX when:{days}d', 'hl': 'en-AU', 'gl': 'AU', 'ceid': 'AU:en'})
    req = urllib.request.Request('https://news.google.com/rss/search?' + q, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r: root = ET.fromstring(r.read())
    out, seen = [], set()
    for it in root.iter('item'):
        src = it.find('source'); sname = (src.text or '').strip() if src is not None else ''; dom = host(src.get('url', '')) if src is not None else ''
        title = (it.findtext('title') or '').strip()
        if sname and title.endswith(' - ' + sname): title = title[:-len(sname) - 3].strip()
        key = re.sub(r'\W+', ' ', title.lower())[:80]
        if not title or key in seen: continue
        seen.add(key)
        try: date = email.utils.parsedate_to_datetime(it.findtext('pubDate')).astimezone(SYD).date().isoformat()
        except Exception: date = ''
        out.append({'title': title, 'source': sname, 'domain': dom, 'url': it.findtext('link') or '', 'date': date})
    return [h for h in out if h['domain'] in TRUSTED][:12]

SYSTEM = ("You explain share price moves to a private investor in plain English. Use ONLY the numbered headlines you are given. Do not use outside knowledge. "
          "Reply with JSON: {\"reason\": string or null, \"support\": [headline numbers], \"headline\": string}. "
          "reason: one or two short sentences (under 45 words), simple words, saying what happened and how it relates to the price move. "
          "Use wording like 'after', 'as' or 'reports say'. Never claim the news definitely caused the move. Do not add numbers that are not in the headlines. "
          "If the headlines do not explain a move in this direction, set reason to null. support lists the headline numbers that back your reason. "
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

def explain(s, p, per):
    """Returns {t,n,move,reason,headline,confidence,sources}. confidence: multiple | single | none."""
    days, w = (3, 'today') if per == 'd' else (8, 'this week')
    move = f"{'rose' if p >= 0 else 'fell'} {abs(p):.1f} percent {w}"
    item = {'t': s['t'], 'n': s['n'], 'move': move, 'reason': None, 'headline': None, 'confidence': 'none', 'sources': []}
    try:
        heads = NEWS_CACHE.setdefault((s['t'], days), gnews(s['n'], s['t'], days))
    except Exception as e:
        print('news fetch failed for', s['t'], repr(e)); return item
    if not heads: print('no trusted headlines for', s['t']); return item
    lines = '\n'.join(f"{i}. [{h['source']}, {h['date']}] {h['title']}" for i, h in enumerate(heads, 1))
    user = f"Company: {s['n']} ({s['t']}). Its share price {move}.\nHeadlines:\n{lines}"
    ans = ask_llm(user)
    if not isinstance(ans, dict) or not ans.get('reason'): return item
    sup = [i for i in ans.get('support', []) if isinstance(i, int) and 1 <= i <= len(heads)]
    if not sup: print('model gave no valid support for', s['t']); return item
    chosen = [heads[i - 1] for i in dict.fromkeys(sup)]
    reason = str(ans['reason']).strip()
    if nums(reason) - (nums(' '.join(h['title'] for h in chosen)) | nums(move)):
        print('dropped an explanation with numbers not found in the headlines for', s['t']); return item
    names = list(dict.fromkeys(h['source'] for h in chosen))
    item.update(reason=reason if reason.endswith('.') else reason + '.', headline=str(ans.get('headline') or s['n'])[:80],
                confidence='multiple' if len(names) >= 2 else 'single',
                sources=[{'title': h['title'], 'source': h['source'], 'url': h['url'], 'date': h['date']} for h in chosen])
    return item

def join_names(xs): return xs[0] if len(xs) == 1 else ', '.join(xs[:-1]) + ' and ' + xs[-1]

def news_say(item, first):
    pre = '' if first else f"{item['n']} {item['move']}. "
    if item['confidence'] == 'none': return pre + f"I could not find a clear reason for {item['n']}'s move in the news."
    names = list(dict.fromkeys(x['source'] for x in item['sources']))[:2]
    tail = f" Several sources report this, including {join_names(names)}." if item['confidence'] == 'multiple' else f" Only one source, {names[0]}, reports this, so treat it with care."
    return pre + item['reason'] + tail

def research(stocks, per):
    days = 1 if per == 'd' else 7
    M = sorted([(s, chg(s['rows'], days)) for s in stocks if chg(s['rows'], days) is not None], key=lambda x: -abs(x[1]))[:2]
    return [explain(s, p, per) for s, p in M]

# test fixtures, used only when NEWS_FAKE=1
FAKE_HEADS = {
    'BHP': [{'title': 'BHP shares slide as China curbs iron ore imports', 'source': 'Reuters', 'domain': 'reuters.com', 'url': 'https://www.reuters.com/x', 'date': '2026-10-09'},
            {'title': 'Iron ore falls after China steps up port checks', 'source': 'AFR', 'domain': 'afr.com', 'url': 'https://www.afr.com/y', 'date': '2026-10-09'}],
    'CBA': [{'title': 'Commonwealth Bank lifts home loan rates', 'source': 'ABC News', 'domain': 'abc.net.au', 'url': 'https://www.abc.net.au/z', 'date': '2026-10-09'}],
    'CSL': [{'title': 'CSL cuts outlook by 12 percent', 'source': 'AFR', 'domain': 'afr.com', 'url': 'https://www.afr.com/w', 'date': '2026-10-09'}],
}
def FAKE_LLM(user):
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
