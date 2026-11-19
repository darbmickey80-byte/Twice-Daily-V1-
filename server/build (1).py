"""Twice Daily broadcast builder.

Runs on GitHub Actions in stages (each stage is its own workflow step so the app can show progress):
  check     decide whether to build a broadcast now
  feeds     gather news feeds, group the same story across outlets, sort into topics
  articles  find a full article for each story (own outlet, other outlets, then a news search)
  summaries have an AI write a spoken summary of each article
  audio     record the whole broadcast as one MP3 in the chosen studio voice
  site      assemble the website: app, episode list, audio, voice samples
"""
import base64, datetime as dt, json, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs
from zoneinfo import ZoneInfo

import feedparser
import requests
import trafilatura

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
SITE = ROOT / "site"
STATE_FILE = ROOT / "state" / "state.json"
CATALOG = json.loads((ROOT / "catalog.json").read_text())
APP_FILES = ["index.html", "manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png",
             "icon-maskable-512.png", "catalog.json", "robots.txt"]

DEFAULTS = {
    "scheduler": True, "timezone": "America/New_York", "length": "full", "summaries": True, "keep": 14,
    "sources": {s["id"]: s["on"] for s in CATALOG["sources"]},
    "topics": {t["id"]: t["on"] for t in CATALOG["topics"]},
    "custom": ["", "", ""],
    "voices": {"am": {"engine": "openai", "voice": "onyx"}, "pm": {"engine": "openai", "voice": "nova"}},
    "anthropicModel": "claude-haiku-5-5", "openaiTextModel": "auto", "openaiVoiceModel": "auto",
    "deleted": [],
}


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    p = ROOT / "config" / "settings.json"
    if p.exists():
        try:
            user = json.loads(p.read_text())
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(s.get(k), dict):
                    s[k].update(v)
                else:
                    s[k] = v
        except Exception as e:
            log(f"Settings file couldn't be read ({e}); using defaults.")
    return s


def log(msg):
    print(msg, flush=True)


def gh_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")
    log(f"[output] {key}={value}")


def save(name, data):
    WORK.mkdir(exist_ok=True)
    (WORK / name).write_text(json.dumps(data))


def load(name, default=None):
    p = WORK / name
    return json.loads(p.read_text()) if p.exists() else default


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"episodes": []}


def _clean_key(v):
    v = (v or "").strip().strip('"').strip("'").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return re.sub(r"\s+", "", v)


for _k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JINA_API_KEY"):
    if os.environ.get(_k) is not None:
        os.environ[_k] = _clean_key(os.environ[_k])
        if not os.environ[_k]:
            del os.environ[_k]

SETTINGS = load_settings()
TZ = ZoneInfo(SETTINGS.get("timezone") or "America/New_York")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def slot_info(slot, day):
    start = dt.datetime(day.year, day.month, day.day, 6 if slot == "am" else 16, tzinfo=TZ)
    prev = start - dt.timedelta(hours=14) if slot == "am" else start - dt.timedelta(hours=10)
    return {"slot": slot, "start": start.isoformat(), "prevStart": prev.isoformat(), "key": f"{start:%Y-%m-%d}-{slot}"}


# ---------------------------------------------------------------- check
def stage_check():
    event = os.environ.get("EVENT", "")
    mode = os.environ.get("MODE") or "full"
    now = dt.datetime.now(TZ)
    state = load_state()
    have = {e["key"] for e in state.get("episodes", [])}
    if event == "workflow_dispatch":
        if mode == "publish":
            gh_output("run", "false"); gh_output("publish", "true"); return
        m = now.hour * 60 + now.minute
        if 4 * 60 + 45 <= m < 14 * 60 + 45:
            info = slot_info("am", now)
        elif m >= 14 * 60 + 45:
            info = slot_info("pm", now)
        else:
            info = slot_info("pm", now - dt.timedelta(days=1))
        info["refresh"] = info["key"] in have
    else:
        if not SETTINGS.get("scheduler", True):
            log("The scheduler is turned off in the app. Nothing to do.")
            gh_output("run", "false"); gh_output("publish", "false"); return
        m = now.hour * 60 + now.minute
        if 4 * 60 + 45 <= m <= 6 * 60 + 45:
            info = slot_info("am", now)
        elif 14 * 60 + 45 <= m <= 16 * 60 + 45:
            info = slot_info("pm", now)
        else:
            log(f"It's {now:%H:%M} where you are, not broadcast-preparation time. Nothing to do.")
            gh_output("run", "false"); gh_output("publish", "false"); return
        if info["key"] in have:
            log("This edition was already published. Nothing to do.")
            gh_output("run", "false"); gh_output("publish", "false"); return
        info["refresh"] = False
    # News window: since the previous broadcast, at most 24 hours
    last = max((e["createdAt"] for e in state.get("episodes", [])), default=0)
    floor = (now - dt.timedelta(hours=24)).timestamp() * 1000
    info["windowStart"] = max(last, floor, 0) if last else dt.datetime.fromisoformat(info["prevStart"]).timestamp() * 1000
    info["createdAt"] = now.timestamp() * 1000
    save("plan.json", info)
    log(f"Building the {info['slot'].upper()} edition ({info['key']}){' as an update' if info['refresh'] else ''}.")
    gh_output("run", "true"); gh_output("publish", "true")


# ---------------------------------------------------------------- text helpers
STOP = set("the a an and or but of to in on at for with from by as is are was were be been it its this that these those after before over under about into than then they their them he she his her you your we our new says said say will would could can has have had not no more most what who how why when where amid just up out off".split())


def tokens(s):
    words = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split()
    return {re.sub(r"s$", "", w) for w in words if len(w) > 2 and w not in STOP}


def similarity(a, b):
    if not a or not b:
        return 0
    i = len(a & b)
    return i / min(len(a), len(b)) * (1 if i >= 3 else 0.5)


def html_paragraphs(html):
    """Feed HTML to plain paragraphs, one per line."""
    s = re.sub(r"(?is)<(script|style|figure|figcaption|aside)[^>]*>.*?</\1>", " ", html or "")
    s = re.sub(r"(?i)</p>|<br\s*/?>|</h\d>|</li>|</blockquote>", "\n", s)
    lines = [html_to_text(l) for l in s.split("\n")]
    return "\n".join(l for l in lines if l)


def html_to_text(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&amp;", "&", s)
    s = re.sub(r"&#39;|&apos;", "'", s)
    s = re.sub(r"&quot;", '"', s)
    return re.sub(r"\s+", " ", s).strip()


def clean_title(t, gn):
    t = html_to_text(t)
    if gn:
        t = re.sub(r"\s+-\s+[^-]{2,60}$", "", t)
    return t.strip()


def clean_desc(s, title):
    s = html_to_text(s)
    s = re.sub(r"The post .{0,200}? appeared first on .{0,80}?\.?$", "", s, flags=re.I).strip()
    if title and similarity(tokens(s), tokens(title)) > 0.7:
        return ""
    return s[:700]


def word_count(s):
    return len((s or "").split())


def kw_regex(k):
    e = re.escape(k)
    flags = 0 if re.fullmatch(r"[A-Z0-9.&]{2,6}", k) else re.I
    return re.compile(r"(?<![A-Za-z0-9])" + e + r"(?![A-Za-z0-9])", flags)


TOPIC_RE = {t["id"]: [kw_regex(k) for k in t["kw"]] for t in CATALOG["topics"]}


def custom_topics():
    out = []
    for i, raw in enumerate(SETTINGS.get("custom") or []):
        parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
        if parts:
            out.append({"id": f"custom{i}", "name": parts[0], "say": parts[0], "custom": True, "re": [kw_regex(p) for p in parts]})
    return out


def hits(res, text):
    return sum(1 for r in res if r.search(text or ""))


def classify(item, enabled, customs):
    best, best_score = None, 0
    for c in customs:
        sc = hits(c["re"], item["title"]) * 5 + hits(c["re"], item["desc"]) * 2
        if sc > best_score:
            best, best_score = c["id"], sc
    for t in CATALOG["topics"]:
        if t["id"] not in enabled:
            continue
        sc = hits(TOPIC_RE[t["id"]], item["title"]) * 2 + hits(TOPIC_RE[t["id"]], item["desc"])
        if item.get("feedTopic") == t["id"]:
            sc += 3
        if item.get("hint") == t["id"]:
            sc += 2
        if t["id"] == "local" and item.get("local"):
            sc += 4
        if sc > best_score:
            best, best_score = t["id"], sc
    if best:
        return best
    if item.get("local") and "local" in enabled:
        return "local"
    return "us" if "us" in enabled else None


# ---------------------------------------------------------------- feeds
def gn_url(domain):
    return "https://news.google.com/rss/search?q=" + quote(f"site:{domain} when:1d") + "&hl=en-US&gl=US&ceid=US:en"


def fetch(url, timeout=15):
    r = SESSION.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r


def fetch_feed(url):
    r = fetch(url)
    d = feedparser.parse(r.content)
    out = []
    for e in d.entries:
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                ts = dt.datetime(*e[key][:6], tzinfo=dt.timezone.utc).timestamp() * 1000
                break
        desc = e.get("summary") or ""
        body = ""
        if e.get("content"):
            body = max((c.get("value", "") for c in e["content"]), key=len, default="")
            if not desc:
                desc = body
        src = (e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None
        out.append({"title": e.get("title", ""), "link": e.get("link", ""), "desc": desc, "date": ts, "srcTitle": src,
                    "body": html_paragraphs(body) if len(body) > 1500 else ""})
    return out


def stage_feeds():
    plan = load("plan.json")
    enabled_sources = [s for s in CATALOG["sources"] if SETTINGS["sources"].get(s["id"], s["on"])]
    jobs = []
    for s in enabled_sources:
        feeds = s["feeds"] or [{"u": gn_url(s["domain"]), "gn": True}]
        for f in feeds:
            jobs.append({"src": s, "u": f["u"], "t": f.get("t"), "gn": bool(f.get("gn")), "backup": False})
    have = {j["u"] for j in jobs}
    for b in CATALOG["backup"]:
        for u in b["feeds"]:
            if u not in have:
                have.add(u)
                jobs.append({"src": {"id": b["id"], "name": b["name"], "domain": ""}, "u": u, "t": None, "gn": False, "backup": True})
    log(f"Reading {len(jobs)} feeds from {len(enabled_sources)} chosen outlets plus backup outlets.")
    ok = set()

    def run(j):
        try:
            items = fetch_feed(j["u"])
            ok.add(j["src"]["id"])
            return [(j, it) for it in items]
        except Exception as e:
            log(f"  feed failed: {j['src']['name']} {j['u'][:80]} ({e.__class__.__name__})")
            return []

    with ThreadPoolExecutor(16) as ex:
        results = [x for r in ex.map(run, jobs) for x in r]
    # Google News fallback for chosen outlets whose own feeds failed
    failed = [s for s in enabled_sources if s["id"] not in ok and s["feeds"]]
    if failed:
        with ThreadPoolExecutor(8) as ex:
            results += [x for r in ex.map(run, [{"src": s, "u": gn_url(s["domain"]), "t": None, "gn": True, "backup": False} for s in failed]) for x in r]
    missing = [s["name"] for s in enabled_sources if s["id"] not in ok]

    items = []
    for j, it in results:
        title = clean_title(it["title"], j["gn"])
        if len(title) < 13:
            continue
        items.append({
            "title": title, "desc": clean_desc(it["desc"], title), "link": it["link"], "date": it["date"], "body": it.get("body", ""),
            "src": j["src"]["name"], "srcId": j["src"]["id"], "gn": j["gn"], "backup": j["backup"],
            "feedTopic": j["t"], "hint": j["src"].get("hint"), "local": bool(j["src"].get("local")),
        })
    if not any(not i["backup"] for i in items):
        raise SystemExit("Couldn't reach any of your news sources.")

    now = plan["createdAt"]
    def in_window(i, start):
        return i["date"] is None or (start <= i["date"] <= now + 10 * 60000)
    mine = [i for i in items if not i["backup"]]
    pool = [i for i in mine if in_window(i, plan["windowStart"])]
    if len(pool) < 25:
        pool = [i for i in mine if in_window(i, now - 24 * 3600000)]

    # Group the same story across outlets
    clusters = []
    for it in pool:
        it["tok"] = tokens(it["title"])
        hit = next((c for c in clusters if similarity(it["tok"], c["rep"]["tok"]) >= 0.6), None)
        if hit:
            hit["items"].append(it)
            rep = hit["rep"]
            if (not it["gn"] and (rep["gn"] or len(it["desc"]) > len(rep["desc"])) and it["srcId"] != rep["srcId"]) or \
               (it["srcId"] == rep["srcId"] and len(it["desc"]) > len(rep["desc"])):
                hit["rep"] = it
        else:
            clusters.append({"rep": it, "items": [it]})

    enabled = {t["id"] for t in CATALOG["topics"] if SETTINGS["topics"].get(t["id"], t["on"])}
    customs = custom_topics()
    by_topic = {}
    for c in clusters:
        rep = dict(c["rep"])
        rep["desc"] = max((i["desc"] for i in c["items"]), key=len, default=rep["desc"])
        topic = classify(rep, enabled, customs)
        if not topic:
            continue
        outlets = list(dict.fromkeys(i["src"] for i in c["items"]))
        seen, direct = set(), []
        for i in [c["rep"]] + [x for x in c["items"] if x["srcId"] != c["rep"]["srcId"]] + c["items"]:
            if i["link"] and i["link"] not in seen:
                seen.add(i["link"])
                direct.append({"link": i["link"], "name": i["src"], "title": i["title"], "gn": i["gn"], "body": i.get("body", "")})
        by_topic.setdefault(topic, []).append({
            "title": rep["title"], "desc": rep["desc"], "outlets": outlets, "coverage": len(outlets),
            "newest": max((i["date"] or 0) for i in c["items"]), "direct": direct[:4],
            "link": next((d["link"] for d in direct if not d["gn"]), rep["link"]), "src": rep["src"],
        })
    order = [t for t in CATALOG["topics"] if t["id"] in enabled] + customs
    ranked = []
    for t in order:
        lst = sorted(by_topic.get(t["id"], []), key=lambda x: (-x["coverage"], -x["newest"]))
        if lst:
            ranked.append({"topic": {k: t[k] for k in ("id", "name", "say")} | {"custom": bool(t.get("custom"))}, "list": lst[:30]})
    lookup = [{"title": i["title"], "link": i["link"], "name": i["src"]} for i in items if not i["gn"] and i["link"].startswith("http")]
    save("candidates.json", {"ranked": ranked, "lookup": lookup, "missing": missing})
    log(f"Found {len(clusters)} distinct stories; {sum(len(r['list']) for r in ranked)} fit your topics.")


# ---------------------------------------------------------------- articles
PAYWALL = re.compile(r"(subscribe to (continue|read)|already a subscriber|subscribers only|create a free account to continue|sign in to continue reading|this content is for subscribers|to continue reading)", re.I)
JUNK_LINE = re.compile(r"^(advertisement|sponsored|story continues|scroll to continue|read more|related|recommended|watch:|sign up|subscribe|get the latest|download (the|our)|follow us|copyright|©|all rights reserved|.{0,60}may (get|earn|receive) a commission|our editors independently)", re.I)

try:  # optional: turns Google News links into the original article links
    from googlenewsdecoder import gnewsdecoder  # type: ignore
except Exception:
    gnewsdecoder = None


def decode_gn(url):
    if not gnewsdecoder or "news.google.com" not in url:
        return None
    try:
        r = gnewsdecoder(url)
        if isinstance(r, dict) and r.get("status") and r.get("decoded_url"):
            return r["decoded_url"]
    except Exception:
        pass
    return None


def clean_lines(text):
    return "\n".join(l.strip() for l in (text or "").split("\n") if l.strip() and not JUNK_LINE.match(l.strip()))


def extract(html, url):
    opts = dict(url=url, include_comments=False, include_tables=False, include_images=False, include_links=False, deduplicate=True)
    text = trafilatura.extract(html, favor_precision=True, **opts) or ""
    if word_count(text) < 250:
        wider = trafilatura.extract(html, favor_recall=True, **opts) or ""
        if word_count(wider) > word_count(text):
            text = wider
    return clean_lines(text)


import threading
STATS = {}
_stats_lock = threading.Lock()
_jina_lock = threading.Lock()
_jina = {"last": 0.0, "count": 0}


def note(link, how):
    host = (urlparse(link).hostname or "?").replace("www.", "")
    with _stats_lock:
        STATS.setdefault(host, {"tried": 0, "direct": 0, "reader": 0, "feed": 0, "failed": 0, "why": set()})
        st = STATS[host]
        if how == "tried":
            st["tried"] += 1
        elif how in ("direct", "reader", "feed"):
            st[how] += 1
        else:
            st["failed"] += 1
            st["why"].add(how)


def jina_markdown_to_text(md):
    body = md.split("Markdown Content:", 1)[-1]
    out = []
    for line in body.split("\n"):
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)            # images
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)        # links -> their text
        line = re.sub(r"^[#>*\-\s]+", "", line).strip()                 # headings, quotes, bullets
        line = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", line)
        if len(line) < 50 and not re.search(r"[.!?\"”]$", line):         # menus, labels, buttons
            continue
        out.append(line)
    return clean_lines("\n".join(out))


def fetch_via_reader(url):
    """Jina Reader opens the page like a browser and returns the article as text."""
    key = os.environ.get("JINA_API_KEY")
    gap = 0.15 if key else float(os.environ.get("JINA_GAP", 3.3))                       # free use without a key: about 18 pages a minute
    with _jina_lock:
        if not key and _jina["count"] >= 160:
            return ""
        wait = _jina["last"] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _jina["last"] = time.time(); _jina["count"] += 1
    headers = {"Accept": "text/plain", "X-Retain-Images": "none", "X-Timeout": "25"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for attempt in range(2):
        try:
            r = requests.get("https://r.jina.ai/" + url, headers=headers, timeout=45)
            if r.status_code == 429 and attempt == 0:
                time.sleep(20); continue
            if not r.ok:
                return ""
            return jina_markdown_to_text(r.text)[:14000]
        except Exception:
            return ""
    return ""


def fetch_wordpress(url):
    try:
        u = urlparse(url)
        slug = [p for p in u.path.split("/") if p][-1]
        if not re.fullmatch(r"[a-z0-9-]{8,}", slug or ""):
            return ""
        r = SESSION.get(f"{u.scheme}://{u.netloc}/wp-json/wp/v2/posts", params={"slug": slug, "_fields": "content"}, timeout=15)
        if not r.ok:
            return ""
        posts = r.json()
        if isinstance(posts, list) and posts:
            return clean_lines(html_paragraphs(posts[0].get("content", {}).get("rendered", "")))[:14000]
    except Exception:
        pass
    return ""


def fetch_article(url):
    note(url, "tried")
    tries = [url]
    m = re.search(r"npr\.org/(?:\d{4}/\d{2}/\d{2}/)?((?:nx-s1-)?\d{6,})", url)
    if m:
        tries.insert(0, f"https://text.npr.org/{m.group(1)}")
    best, why = "", "no article text"
    for u in tries:
        try:
            r = fetch(u, timeout=15)
            if "html" not in r.headers.get("content-type", "html"):
                continue
            text = extract(r.text, r.url)
            if word_count(text) > word_count(best):
                best = text
            if is_full(best):
                note(url, "direct"); return best[:14000]
        except requests.HTTPError as e:
            why = f"blocked ({e.response.status_code if e.response is not None else 'error'})"
        except Exception as e:
            why = e.__class__.__name__
    wp = fetch_wordpress(url)
    if is_full(wp):
        note(url, "direct"); return wp
    text = fetch_via_reader(url)
    if is_full(text):
        note(url, "reader"); return text
    note(url, why if not best else "too short or paywalled")
    return best[:14000] if word_count(best) >= word_count(text) else text


def is_full(text):
    if not text or word_count(text) < 200:
        return False
    if PAYWALL.search(text[-600:]) and word_count(text) < 450:
        return False
    return len(re.findall(r"[.!?][\"”’)]?\s", text)) >= 5


def relevant(title, text, alt=None):
    body = tokens(text[:9000])
    def score(t):
        t = list(tokens(t))
        return 1 if len(t) < 3 else sum(1 for w in t if w in body) / len(t)
    return max(score(title), score(alt) if alt else 0) >= 0.5


def outlet_name(link):
    try:
        h = urlparse(link).hostname.replace("www.", "")
        for s in CATALOG["sources"]:
            if h == s["domain"] or h.endswith("." + s["domain"]):
                return s["name"]
        base = h.split(".")[-2] if "." in h else h
        return base.capitalize()
    except Exception:
        return "another outlet"


def search_other_outlets(title):
    q = re.sub(r"[“”\"'‘’]", "", title)
    q = " ".join(q.split()[:14])
    out = []
    try:
        d = feedparser.parse(fetch("https://www.bing.com/news/search?q=" + quote(q) + "&format=rss&setlang=en-us&cc=us", timeout=12).content)
        for e in d.entries[:8]:
            link = e.get("link", "")
            real = parse_qs(urlparse(link).query).get("url")
            if real:
                link = real[0]
            if not link.startswith("http") or re.search(r"bing\.com|msn\.com|news\.google", link):
                continue
            name = e.get("news_source") or ((e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None)
            out.append({"link": link, "title": html_to_text(e.get("title", "")), "name": name or outlet_name(link)})
    except Exception as ex:
        log(f"  search failed for '{q[:40]}' ({ex.__class__.__name__})")
    return out


def find_full_text(st, lookup):
    tok = tokens(st["title"])
    tried = set()

    def attempt(link, name, alt=None, own=False):
        if not link or link in tried or len(tried) >= 8:
            return False
        tried.add(link)
        text = fetch_article(link)
        if is_full(text) and (own or relevant(st["title"], text, alt)):
            st["article"], st["credit"] = text, name
            return True
        return False

    for d in st["direct"]:
        body = clean_lines(d.get("body", ""))
        if is_full(body):
            note(d["link"], "tried"); note(d["link"], "feed")
            st["article"], st["credit"] = body[:14000], d["name"]
            return True
    for d in st["direct"]:
        link = decode_gn(d["link"]) if d["gn"] else d["link"]
        if link and attempt(link, d["name"], d["title"], own=True):
            return True
    others = sorted((b for b in lookup if b["link"] not in tried and similarity(tok, tokens(b["title"])) >= 0.5),
                    key=lambda b: -similarity(tok, tokens(b["title"])))[:3]
    for b in others:
        if attempt(b["link"], b["name"], b["title"]):
            return True
    for f in [f for f in search_other_outlets(st["title"]) if similarity(tok, tokens(f["title"])) >= 0.5][:4]:
        if attempt(f["link"], f["name"], f["title"]):
            return True
    return False


def per_topic():
    return {"quick": 3, "standard": 4, "full": 6}.get(SETTINGS.get("length"), 4)


def use_ai():
    return bool(SETTINGS.get("summaries", True) and (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")))


def stage_articles():
    cand = load("candidates.json")
    keep = per_topic()
    spare = 1 if use_ai() else 0
    need = lambda t: (min(keep, 4) if t["custom"] else keep) + spare
    ranked, lookup = cand["ranked"], cand["lookup"]
    started = time.time()
    for rnd in range(3):
        batch = []
        for r in ranked:
            want = need(r["topic"]) - sum(1 for s in r["list"] if s.get("full"))
            if want > 0:
                batch += [s for s in r["list"] if "full" not in s][: (want + 2) if rnd == 0 else want * 2]
        if not batch:
            break
        log(f"Round {rnd + 1}: checking {len(batch)} stories for full articles.")
        def work(st):
            st["full"] = find_full_text(st, lookup)
            return st
        with ThreadPoolExecutor(12) as ex:
            list(ex.map(work, batch))
    sections, left_out = [], 0
    for r in ranked:
        left_out += sum(1 for s in r["list"] if s.get("full") is False)
        lst = [s for s in r["list"] if s.get("full")][: need(r["topic"])]
        if lst:
            sections.append({"topic": r["topic"], "stories": lst})
    found = sum(len(s["stories"]) for s in sections)
    log(f"Found full articles for {found} stories in {time.time() - started:.0f} seconds; {left_out} left out.")
    log("Article reading by website (tried / read directly / from feed / read through Reader / failed):")
    unreadable = []
    for host, st in sorted(STATS.items(), key=lambda x: -x[1]["tried"]):
        log(f"  {host}: {st['tried']} / {st['direct']} / {st['feed']} / {st['reader']} / {st['failed']}  {', '.join(sorted(st['why']))}")
        if st["tried"] >= 2 and not st["direct"] and not st["reader"] and not st["feed"]:
            unreadable.append(outlet_name("https://" + host))
    if not sections:
        raise SystemExit("Couldn't find full articles for any stories.")
    save("stories.json", {"sections": sections, "leftOut": left_out, "missing": cand["missing"], "unreadable": unreadable[:6]})


# ---------------------------------------------------------------- summaries
def take_words(text, n):
    out, count = [], 0
    for p in [p for p in text.split("\n") if p.strip()]:
        w = word_count(p)
        if count and count + w > n * 1.15:
            break
        out.append(p); count += w
        if count >= n:
            break
    s = " ".join(out)
    if count > n * 1.3:
        cut = " ".join(s.split()[: int(n * 1.1)])
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        s = cut[: end + 1] if end > 40 else cut + "."
    return s


def free_script(st, words):
    t = st["title"].strip()
    if not re.search(r"[.!?\"”]$", t):
        t += "."
    text = f"From {st['credit']}: {t} {take_words(st['article'], words)}"
    others = [o for o in st["outlets"] if o != st["credit"]][:3]
    if others:
        text += f" {list_words(others)} {'is' if len(others) == 1 else 'are'} also reporting on this story."
    return text


def list_words(a):
    a = list(a)
    if len(a) <= 1:
        return "".join(a)
    if len(a) == 2:
        return f"{a[0]} and {a[1]}"
    return ", ".join(a[:-1]) + ", and " + a[-1]


class ApiError(RuntimeError):
    def __init__(self, who, status, msg, code=""):
        super().__init__(f"{who} error {status}: {msg}")
        self.who, self.status, self.msg, self.code = who, status, msg, code

    def plain(self):
        if self.status == 401:
            return f"your {self.who} key was rejected ({self.msg})"
        if self.status == 429 and ("quota" in self.code or "quota" in self.msg.lower() or "billing" in self.msg.lower()):
            return f"your {self.who} account is out of credit; add credit in {self.who}'s billing settings"
        if self.status == 403:
            return f"your {self.who} key isn't allowed to do this ({self.msg}); make sure the key's permissions are set to All"
        return f"{self.who} returned an error ({self.status}: {self.msg})"


def api_error(r, who):
    code = ""
    try:
        err = r.json().get("error", {})
        msg, code = (err.get("message") or "")[:200], (err.get("code") or err.get("type") or "")
    except Exception:
        msg = r.text[:200]
    return ApiError(who, r.status_code, msg, str(code))


_oa_models = None


def openai_models():
    global _oa_models
    if _oa_models is None:
        r = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=30)
        if not r.ok:
            raise api_error(r, "OpenAI")
        _oa_models = [m["id"] for m in r.json().get("data", [])]
    return _oa_models


def ver(mid):
    m = re.search(r"gpt-(\d+(?:\.\d+)?)", mid)
    return float(m.group(1)) if m else 0


def newest(ids):
    return sorted(ids, key=ver, reverse=True)[0]


def dated(mid):
    return bool(re.search(r"-\d{4}-\d{2}-\d{2}$|-\d{4}$", mid))


def text_model():
    if SETTINGS.get("openaiTextModel") not in (None, "", "auto"):
        return SETTINGS["openaiTextModel"]
    ids = [m for m in openai_models() if not dated(m) and not re.search(r"audio|realtime|tts|transcribe|search|image|embedding|codex|instruct|preview|moderation|whisper|dall-e|sora|chat-latest", m, re.I)]
    for pat in [r"^gpt-[\d.]+-luna$", r"^gpt-[\d.]+-mini$", r"^gpt-[\d.]+-terra$", r"^gpt-[\d.]+-nano$", r"^gpt-4o-mini$"]:
        c = [m for m in ids if re.match(pat, m)]
        if c:
            return newest(c)
    raise RuntimeError("no suitable OpenAI summary model found")


def voice_model():
    if SETTINGS.get("openaiVoiceModel") not in (None, "", "auto"):
        return SETTINGS["openaiVoiceModel"]
    try:
        ids = [m for m in openai_models() if "tts" in m and not dated(m)]
        g = [m for m in ids if m.startswith("gpt")]
        return newest(g) if g else ("tts-1-hd" if "tts-1-hd" in ids else (ids[0] if ids else "gpt-4o-mini-tts"))
    except Exception:
        return "gpt-4o-mini-tts"


def parse_json(text):
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise RuntimeError("unexpected reply")
    return json.loads(m.group(0))


def call_openai(prompt, max_tokens):
    r = requests.post("https://api.openai.com/v1/chat/completions", timeout=300,
                      headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
                      json={"model": text_model(), "max_completion_tokens": min(32000, max_tokens + 8000),
                            "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]})
    if not r.ok:
        raise api_error(r, "OpenAI")
    return parse_json(r.json()["choices"][0]["message"].get("content", ""))


def call_claude(prompt, max_tokens):
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=300,
                      headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": SETTINGS.get("anthropicModel") or "claude-haiku-5-5", "max_tokens": max_tokens,
                            "messages": [{"role": "user", "content": prompt}]})
    if not r.ok:
        raise api_error(r, "Claude")
    return parse_json("".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"))


def summarize_section(sec, words, call):
    lo, hi, sents = round(words * 0.85), round(words * 1.2), max(4, round(words / 17))
    items = [{"id": k, "outlet": st["credit"], "also_reporting": [o for o in st["outlets"] if o != st["credit"]][:3],
              "headline": st["title"], "feed_summary": st.get("desc", ""), "article": st["article"][:9000]}
             for k, st in enumerate(sec["stories"])]
    prompt = (
        f"You write spoken summaries for Twice Daily, a personal news podcast that a text-to-speech voice reads aloud. Section: {sec['topic']['name']}.\n\n"
        f"For each story below, write a summary of {lo} to {hi} words (about {sents} sentences) that a listener can follow by ear.\n"
        f"- Length matters. The broadcast is timed, so a summary shorter than {lo} words makes it run short.\n"
        "- Build each summary like this: the main news in one or two sentences; then the key details, people and figures; then background, context or reactions reported in the article; then what happens next.\n"
        "- Use only facts from that story's article, headline and feed summary. Never add outside facts, names, figures, or speculation.\n"
        "- The article text may still contain leftovers from the web page: advertisements, sponsored or shopping content, newsletter or app sign-ups, links to other stories, photo captions, or affiliate notices. Ignore all of that and summarize only the news story itself.\n"
        "- If a story's article text isn't really a full news article (for example it's only a teaser, a paywall notice, a video or photo page, an ad, or a shopping deal), don't summarize it. Return {\"id\": number, \"skip\": true} for that story instead.\n"
        "- Credit the outlet once, naturally, near the start (for example, \"NPR reports that...\"). You may briefly note other outlets that are also reporting it.\n"
        "- Calm, warm, professional anchor tone. Plain spoken sentences: no headings, lists, emoji, markdown, or long direct quotes. Say figures and abbreviations the way an anchor would.\n"
        "- Each summary stands alone. Don't mention \"this article\" or the other stories.\n\n"
        "Reply with only JSON: {\"stories\": [{\"id\": number, \"text\": string}]}\n\nStories:\n" + json.dumps(items))
    out = call(prompt, min(16000, 1500 + len(items) * hi * 2))
    n = 0
    for s in out.get("stories", []):
        st = sec["stories"][s["id"]] if isinstance(s.get("id"), int) and s["id"] < len(sec["stories"]) else None
        if not st:
            continue
        if s.get("skip") is True:
            st["skip"] = True
        elif isinstance(s.get("text"), str) and len(s["text"].strip()) > 40:
            st["text"] = s["text"].strip(); n += 1
    short = [(k, st) for k, st in enumerate(sec["stories"]) if not st.get("skip") and word_count(st.get("text")) < words * 0.75]
    if short:
        p2 = (f"These spoken news summaries for a timed podcast came out too short. Rewrite each one to {lo} to {hi} words (about {sents} sentences), "
              "adding more of the specific details, figures, context, reactions and next steps from its article. Use only facts from the article, and ignore "
              "any advertisements, sign-ups, captions or links to other stories in it. Keep a calm anchor tone and plain spoken sentences, and credit the outlet once near the start.\n\n"
              "Reply with only JSON: {\"stories\": [{\"id\": number, \"text\": string}]}\n\nStories:\n" +
              json.dumps([{"id": k, "outlet": st["credit"], "headline": st["title"], "current_summary": st.get("text", ""), "article": st["article"][:9000]} for k, st in short]))
        try:
            for s in call(p2, min(16000, 1500 + len(short) * hi * 2)).get("stories", []):
                st = sec["stories"][s["id"]] if isinstance(s.get("id"), int) and s["id"] < len(sec["stories"]) else None
                if st and isinstance(s.get("text"), str) and word_count(s["text"]) > word_count(st.get("text")):
                    st["text"] = s["text"].strip()
        except Exception as e:
            log(f"  lengthening pass failed: {e}")
    return n


def stage_summaries():
    plan, data = load("plan.json"), load("stories.json")
    sections = data["sections"]
    keep = per_topic()
    keep_for = lambda t: min(keep, 4) if t["custom"] else keep
    expected = sum(min(len(s["stories"]), keep_for(s["topic"])) for s in sections)
    target = {"quick": 10, "standard": 20, "full": 30}.get(SETTINGS.get("length"), 20)
    words = max(80, min(280, round((target * 160 - 180 - len(sections) * 8) / max(1, expected))))
    warn = []
    if data["leftOut"]:
        warn.append(f"{data['leftOut']} {'story was' if data['leftOut'] == 1 else 'stories were'} left out because no outlet had the full article.")
    if data.get("unreadable"):
        warn.append(f"Couldn't read articles from {list_words(data['unreadable'])}.")
    for sec in sections:
        for st in sec["stories"]:
            st["text"] = free_script(st, words)
    providers, summarizer, written = [], "", 0
    if use_ai():
        if os.environ.get("ANTHROPIC_API_KEY"):
            providers.append(("Claude", call_claude))
        if os.environ.get("OPENAI_API_KEY"):
            providers.append(("OpenAI", call_openai))
    if providers:
        bad, errors = set(), []
        def work(sec):
            nonlocal summarizer
            for name, call in providers:
                if name in bad:
                    continue
                try:
                    n = summarize_section(sec, words, call)
                    summarizer = summarizer or name
                    return n
                except Exception as e:
                    errors.append(e.plain() if isinstance(e, ApiError) else f"{name}: {e}")
                    log(f"  summaries failed for {sec['topic']['name']} with {name}: {e}")
                    if isinstance(e, ApiError) and (e.status in (401, 403) or (e.status == 429 and "credit" in e.plain())):
                        bad.add(name)
            return 0
        with ThreadPoolExecutor(3) as ex:
            written = sum(ex.map(work, sections))
        if not written:
            reason = errors[0] if errors else "unknown problem"
            warn.append(f"Summaries couldn't be written because {reason}. This edition reads each article's opening paragraphs.")
    final = []
    for sec in sections:
        stories = [s for s in sec["stories"] if not s.get("skip")][: keep_for(sec["topic"])]
        if stories:
            final.append({"topic": sec["topic"], "stories": stories})
    if not final:
        raise SystemExit("No stories were left after checking the articles.")

    start = dt.datetime.fromisoformat(plan["start"])
    created = dt.datetime.fromtimestamp(plan["createdAt"] / 1000, TZ)
    am = plan["slot"] == "am"
    count = sum(len(s["stories"]) for s in final)
    outlets = list(dict.fromkeys(st["credit"] for s in final for st in s["stories"]))
    day = WEEKDAYS[start.weekday()]
    intro = (f"{'Good morning' if am else 'Good afternoon'}. It's {day}, {MONTHS[start.month - 1]} {ordinal(start.day)}. "
             f"This is Twice Daily, your {'6 A.M. briefing' if am else '4 P.M. update'}"
             + (f", updated at {created:%-I:%M} {'A.M.' if created.hour < 12 else 'P.M.'}" if plan["refresh"] else "") + ". "
             f"Here are {count} stories {'since yesterday afternoon' if am else 'since this morning'}, from {list_words(outlets)}. "
             f"Coming up: {list_words([s['topic']['say'] for s in final]).rstrip('.')}.")
    segs = [{"type": "intro", "title": "Introduction", "text": intro}]
    leads = ["Now to", "Turning to", "Next,", "In", "Moving on to"]
    for i, sec in enumerate(final):
        say = sec["topic"]["say"].rstrip(".")
        segs.append({"type": "topic", "topic": sec["topic"]["name"], "title": sec["topic"]["name"],
                     "text": (f"We begin with {say}" if i == 0 else f"{leads[(i - 1) % len(leads)]} {say}") + "."})
        for st in sec["stories"]:
            segs.append({"type": "story", "topic": sec["topic"]["name"], "source": st["credit"], "title": st["title"],
                         "also": [o for o in st["outlets"] if o != st["credit"]][:3], "link": st["link"], "text": st["text"]})
    segs.append({"type": "outro", "title": "Wrap-up",
                 "text": f"That's your {'morning briefing' if am else 'afternoon update'} for {day}. "
                         f"{'Your afternoon update will be ready at 4 P.M.' if am else 'Your morning briefing will be ready at 6 A.M.'} Thanks for listening."})
    ep = {"id": f"ep{int(plan['createdAt'])}", "key": plan["key"], "slot": plan["slot"], "refresh": plan["refresh"],
          "createdAt": plan["createdAt"], "editionDate": start.timestamp() * 1000, "segments": segs, "outlets": outlets,
          "missing": data["missing"], "warn": warn, "summarizer": summarizer, "stories": count, "target": target}
    save("episode.json", ep)
    log(f"Script ready: {count} stories, about {sum(word_count(s['text']) for s in segs)} words.")


# ---------------------------------------------------------------- audio
def speech_text(t):
    t = re.sub(r"https?://\S+", "", t or "")
    t = re.sub(r"(?<![A-Za-z.])AI(?![A-Za-z])", "A.I.", t)
    return re.sub(r"\s+", " ", t.replace(" & ", " and ")).strip()


def chunks(t, limit=3500):
    sents = re.findall(r"[^.!?]+(?:[.!?]+[\"”’)]*|$)", t) or [t]
    out, cur = [], ""
    for s in sents:
        if len(cur) + len(s) > limit and cur:
            out.append(cur.strip()); cur = s
        else:
            cur += s
    if cur.strip():
        out.append(cur.strip())
    return out


def tts(text, voice, model, attempt=0):
    body = {"model": model, "voice": voice, "input": text, "response_format": "mp3"}
    if model.startswith("gpt"):
        body["instructions"] = "Read like a calm, warm, professional public-radio news anchor. Clear diction, natural pacing, short pauses between sentences."
    try:
        r = requests.post("https://api.openai.com/v1/audio/speech", timeout=180, json=body,
                          headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    except requests.RequestException:
        if attempt < 3:
            time.sleep(3 * (attempt + 1)); return tts(text, voice, model, attempt + 1)
        raise
    if (r.status_code == 429 or r.status_code >= 500) and attempt < 4:
        time.sleep(4 * (attempt + 1)); return tts(text, voice, model, attempt + 1)
    if not r.ok:
        raise api_error(r, "OpenAI")
    return r.content


def mp3_frame(b, o):
    if o + 4 > len(b) or b[o] != 0xFF or (b[o + 1] & 0xE0) != 0xE0:
        return None
    ver, layer = (b[o + 1] >> 3) & 3, (b[o + 1] >> 1) & 3
    if ver == 1 or layer != 1:
        return None
    bri, sri, pad = b[o + 2] >> 4, (b[o + 2] >> 2) & 3, (b[o + 2] >> 1) & 1
    if bri in (0, 15) or sri == 3:
        return None
    br1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    br2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
    sr = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}[ver][sri]
    br = (br1 if ver == 3 else br2)[bri] * 1000
    length = (144 * br // sr if ver == 3 else 72 * br // sr) + pad
    return (length, sr, 1152 if ver == 3 else 576) if length > 4 else None


def mp3_clean(b):
    """Strip tags and the header frame from one MP3 clip so clips join cleanly; return (bytes, seconds)."""
    o, end = 0, len(b)
    if b[:3] == b"ID3":
        o = 10 + (((b[6] & 0x7f) << 21) | ((b[7] & 0x7f) << 14) | ((b[8] & 0x7f) << 7) | (b[9] & 0x7f)) + (10 if b[5] & 0x10 else 0)
    if end > 128 and b[end - 128:end - 125] == b"TAG":
        end -= 128
    while o < end - 4 and not mp3_frame(b, o):
        o += 1
    start, seconds, frames, first = o, 0.0, 0, True
    while o < end - 4:
        f = mp3_frame(b, o)
        if not f:
            break
        length, sr, spf = f
        if first:
            first = False
            if re.search(rb"Xing|Info|VBRI", b[o + 4:o + min(length, 64)]):
                o += length; start = o; continue
        seconds += spf / sr; frames += 1; o += length
    if not frames:
        return b, len(b) / 16000
    return b[start:min(o, end)], seconds


def duration(path):
    if shutil.which("ffprobe"):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        try:
            return float(out.stdout.strip())
        except ValueError:
            pass
    return mp3_clean(Path(path).read_bytes())[1]


def stage_audio():
    ep = load("episode.json")
    choice = (SETTINGS.get("voices") or {}).get(ep["slot"]) or {}
    if choice.get("engine") != "openai" or not os.environ.get("OPENAI_API_KEY"):
        log("This edition uses a phone voice, so there's nothing to record.")
        return
    voice, model = choice.get("voice") or "onyx", voice_model()
    units = [(i, c) for i, s in enumerate(ep["segments"]) for c in chunks(speech_text(s["text"]))]
    parts = WORK / "parts"
    shutil.rmtree(parts, ignore_errors=True); parts.mkdir(parents=True)
    log(f"Recording {len(units)} passages with the {voice} voice ({model}).")
    def work(k):
        (parts / f"{k:04d}.mp3").write_bytes(tts(units[k][1], voice, model))
    try:
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(work, range(len(units))))
        offsets, t, joined = [], 0.0, bytearray()
        for k, (seg, _) in enumerate(units):
            data, secs = mp3_clean((parts / f"{k:04d}.mp3").read_bytes())
            offsets.append({"seg": seg, "t": round(t, 3)})
            joined += data; t += secs
        (WORK / "episode.mp3").write_bytes(bytes(joined))
        ep["audio"] = True
        ep["offsets"] = offsets
        ep["duration"] = round(t, 1)
        ep["voice"] = voice
        mins = max(1, round(ep["duration"] / 60))
        ep["warn"].append(f"This edition runs {mins} minute{'' if mins == 1 else 's'}.")
        log(f"Recorded {ep['duration'] / 60:.1f} minutes of audio.")
    except Exception as e:
        log(f"Recording failed ({e}). The episode will be published without studio audio.")
        reason = e.plain() if isinstance(e, ApiError) else str(e)[:160]
        ep["warn"].append(f"The studio recording failed because {reason}, so this edition uses a phone voice.")
    save("episode.json", ep)


# ---------------------------------------------------------------- site
def pages_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "owner/twice-daily")
    owner, name = repo.split("/")
    return f"https://{owner}.github.io/" if name.lower() == f"{owner.lower()}.github.io" else f"https://{owner}.github.io/{name}/"


def stage_site():
    shutil.rmtree(SITE, ignore_errors=True)
    (SITE / "episodes").mkdir(parents=True)
    (SITE / "voices").mkdir()
    for f in APP_FILES:
        if (ROOT / f).exists():
            shutil.copy(ROOT / f, SITE / f)
    base = pages_url()
    try:
        prev = SESSION.get(base + "episodes/index.json", timeout=30).json()
        if not isinstance(prev, list):
            prev = []
    except Exception:
        prev = []
    deleted = set(SETTINGS.get("deleted") or [])
    new = load("episode.json") if (WORK / "episode.json").exists() else None
    entries = []
    if new and new["id"] not in deleted:
        (SITE / "episodes" / f"{new['id']}.json").write_text(json.dumps(new))
        if new.get("audio"):
            shutil.copy(WORK / "episode.mp3", SITE / "episodes" / f"{new['id']}.mp3")
        entries.append(summary_entry(new))
    keep = max(2, min(30, int(SETTINGS.get("keep") or 14)))
    for e in prev:
        if len(entries) >= keep:
            break
        if e.get("id") in deleted or any(x["id"] == e.get("id") for x in entries):
            continue
        try:
            body = SESSION.get(base + f"episodes/{e['id']}.json", timeout=60)
            body.raise_for_status()
            (SITE / "episodes" / f"{e['id']}.json").write_bytes(body.content)
            if e.get("audio"):
                a = SESSION.get(base + f"episodes/{e['id']}.mp3", timeout=180)
                a.raise_for_status()
                (SITE / "episodes" / f"{e['id']}.mp3").write_bytes(a.content)
            entries.append(e)
        except Exception as ex:
            log(f"  couldn't carry over {e.get('id')} ({ex.__class__.__name__})")
    (SITE / "episodes" / "index.json").write_text(json.dumps(entries))
    # Voice samples for the audition screen (made once, then carried over)
    sample = "Good morning. This is Twice Daily. Here's what's happening across the country and around the world, starting with the latest from Washington."
    for v in CATALOG["studioVoices"]:
        dest = SITE / "voices" / f"{v['id']}.mp3"
        try:
            r = SESSION.get(base + f"voices/{v['id']}.mp3", timeout=30)
            if r.ok and len(r.content) > 2000:
                dest.write_bytes(r.content); continue
        except Exception:
            pass
        if os.environ.get("OPENAI_API_KEY"):
            try:
                dest.write_bytes(tts(sample, v["id"], voice_model()))
            except Exception as ex:
                log(f"  voice sample failed for {v['id']}: {ex}")
    # Remember published editions in the repository (this also keeps the schedule active)
    state = load_state()
    state["episodes"] = [{"id": e["id"], "key": e["key"], "createdAt": e["createdAt"]} for e in entries]
    state["lastRun"] = dt.datetime.now(TZ).isoformat()
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))
    log(f"Site ready with {len(entries)} episodes.")


def summary_entry(ep):
    return {k: ep.get(k) for k in ("id", "key", "slot", "refresh", "createdAt", "editionDate", "stories", "summarizer", "warn", "outlets", "audio", "duration", "voice", "target")}


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    {"check": stage_check, "feeds": stage_feeds, "articles": stage_articles, "summaries": stage_summaries,
     "audio": stage_audio, "site": stage_site}[stage]()
