"""
War-news bot: fetch -> filter -> cluster -> cross-verify -> LLM fact-check -> Telegram.
Everything used here is free (RSS, Gemini free tier, Telegram Bot API, GitHub Actions).
"""
import os, re, json, time, html, calendar
from difflib import SequenceMatcher
from urllib.parse import quote_plus
import feedparser, requests

# ----------------------------- CONFIG ---------------------------------
GEMINI_KEY = os.getenv("AQ.Ab8RN6JlxHXRFDLEUWMaEKoOOBnQ9l2T8qmViJBLG0sQ-N7MHA", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TG_TOKEN = os.getenv("8548970816:AAGxamr37JD4SPb40WAVJkHpQRKxOlHrCSM", "")
TG_CHAT = os.getenv("@war_news02", "")          # e.g. @mychannel
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

MAX_AGE_HOURS = 8        # ignore older news ("latest only")
MAX_POSTS_PER_RUN = 4    # avoid spamming the channel
MAX_LLM_CALLS = 8        # protect the free-tier quota
MIN_IMPORTANCE = 3       # 1-5, set by the LLM
MIN_CONFIDENCE = 70      # 0-100 credibility score from the LLM
DEDUP_HOURS = 48
STATE_FILE = "state.json"

# Conflict topics (searched on Google News RSS, restricted to wire agencies)
TOPICS = [
    "Ukraine Russia war", "Gaza Israel war", "Sudan war", "Myanmar conflict",
    "Yemen Houthi", "Syria conflict", "Congo M23 fighting", "Sahel jihadist attack",
    "Iran Israel strikes", "Lebanon Hezbollah", "Taiwan military", "Ethiopia conflict",
]
WIRE_SITES = ["reuters.com", "apnews.com"]

# Direct RSS feeds (general world news; filtered by war keywords)
DIRECT_FEEDS = {
    "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "DW": "https://rss.dw.com/rdf/rss-en-all",
    "France 24": "https://www.france24.com/en/rss",
}

# Tier-1 outlets: one of them alone is enough to pass the source gate
TIER1 = ["reuters", "associated press", "ap news", "afp", "bbc", "al jazeera", "the guardian"]

WAR_RE = re.compile(
    r"\b(war|attack|strike|strikes|missile|drone|shelling|troops|offensive|ceasefire|"
    r"airstrike|killed|military|frontline|invasion|bomb|bombing|casualt\w*|hostage|"
    r"rebel\w*|militia|army|clashes|artillery|truce|front line|sanctions)\b", re.I)
STOP = set("the a an and or of to in on for with from by at as is are was were be been "
           "after over says said will new amid into its their his her than that this "
           "have has had not but more about up out".split())
UA = {"User-Agent": "Mozilla/5.0 (war-news-bot)"}


# ----------------------------- HELPERS --------------------------------
def tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3 and w not in STOP}

def similar(a, b):
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / max(1, len(ta | tb))
    return jac >= 0.40 or SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.62

def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()

def entry_ts(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(t) if t else None

def is_tier1(name):
    return any(t in name.lower() for t in TIER1)

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"posted": []}

def save_state(state):
    cutoff = time.time() - 3 * 86400
    state["posted"] = [p for p in state["posted"] if p["ts"] > cutoff]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# ----------------------------- 1. FETCH -------------------------------
def fetch_all():
    items, now = [], time.time()

    def add(feed, default_pub=None):
        for e in feed.entries:
            ts = entry_ts(e)
            if not ts or now - ts > MAX_AGE_HOURS * 3600:
                continue
            title = clean(e.get("title"))
            pub = default_pub
            src = e.get("source")
            if src and src.get("title"):                 # Google News gives real publisher
                pub = src["title"]
                title = re.sub(r"\s+-\s+" + re.escape(pub) + r"\s*$", "", title)
            if not pub:
                continue
            text = f"{title} {clean(e.get('summary'))}"
            if not WAR_RE.search(text):
                continue
            items.append({"title": title, "link": e.get("link", ""), "publisher": pub,
                          "ts": ts, "summary": clean(e.get("summary"))[:400]})

    for name, url in DIRECT_FEEDS.items():
        try:
            add(feedparser.parse(url, request_headers=UA), name)
        except Exception as ex:
            print(f"[warn] {name}: {ex}")

    sites = " OR ".join(f"site:{s}" for s in WIRE_SITES)
    for topic in TOPICS:
        q = quote_plus(f"{topic} ({sites}) when:1d")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        try:
            add(feedparser.parse(url, request_headers=UA))
        except Exception as ex:
            print(f"[warn] topic {topic}: {ex}")
    print(f"[info] fetched {len(items)} fresh war-related items")
    return items


# ----------------------------- 2. CLUSTER -----------------------------
def cluster(items):
    clusters = []
    for it in sorted(items, key=lambda x: -x["ts"]):
        for c in clusters:
            if similar(it["title"], c["items"][0]["title"]):
                c["items"].append(it)
                break
        else:
            clusters.append({"items": [it]})
    for c in clusters:
        pubs = {i["publisher"].lower(): i["publisher"] for i in c["items"]}
        c["publishers"] = list(pubs.values())
        c["tier1"] = [p for p in c["publishers"] if is_tier1(p)]
        c["title"] = c["items"][0]["title"]
        c["latest"] = max(i["ts"] for i in c["items"])
    return clusters


# ----------------------------- 3. LLM CHECK ---------------------------
PROMPT = """You are a strict conflict-news verification editor.
Below are reports on ONE story from several outlets. Use ONLY this text. Do NOT add outside facts.

REPORTS:
{reports}

Independent outlets reporting it: {n_pub} ({pubs})

Tasks:
1. Is this a genuine, specific war/armed-conflict development (not opinion, analysis, history, or a recap)?
2. Do the outlets agree on the core facts? Flag contradictions (e.g. very different casualty numbers).
3. Classify claim_type:
   - "confirmed"  : stated as fact by multiple independent outlets or an on-the-ground wire report
   - "attributed" : mainly a claim by one party (a ministry, army, group) reported by outlets
   - "unverified" : single weak source, rumor wording, social-media-only, or suspiciously sensational
4. Red flags for fakeness: sensational wording, no named source, round/implausible numbers, outlets contradicting each other.

Return ONLY JSON:
{{"is_war_news": bool,
  "region": "short place/conflict name",
  "claim_type": "confirmed|attributed|unverified",
  "contradiction": bool,
  "fake_risk": "low|medium|high",
  "confidence": 0-100,
  "importance": 1-5 (5 = major escalation / mass casualties / ceasefire / leader-level event; 1 = minor),
  "headline": "neutral headline in your own words, max 100 chars",
  "summary": "2-3 neutral sentences in your own words; attribute claims ('Russia's defence ministry said...')",
  "reason": "one sentence explaining the verdict"}}"""

def gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
    for attempt in range(3):
        r = requests.post(url, json=body, timeout=60,
                          headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"})
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1)); continue
        r.raise_for_status()
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(txt)
    raise RuntimeError("Gemini rate-limited")

def analyze(c):
    reports = "\n".join(f"- [{i['publisher']}] {i['title']}. {i['summary']}" for i in c["items"][:8])
    return gemini(PROMPT.format(reports=reports, n_pub=len(c["publishers"]),
                                pubs=", ".join(c["publishers"][:8])))


# ----------------------------- 4. PUBLISH -----------------------------
def format_msg(c, a):
    badge = {"confirmed": "✅ Confirmed by multiple sources",
             "attributed": "⚠️ Claim by one party – not independently confirmed",
             "unverified": "❓ Unverified"}[a["claim_type"]]
    links, seen = [], set()
    for i in sorted(c["items"], key=lambda x: not is_tier1(x["publisher"])):
        if i["publisher"] in seen:
            continue
        seen.add(i["publisher"])
        links.append(f'<a href="{html.escape(i["link"], quote=True)}">{html.escape(i["publisher"])}</a>')
        if len(links) == 3:
            break
    return (f"🔴 <b>{html.escape(a['headline'])}</b>\n\n"
            f"{html.escape(a['summary'])}\n\n"
            f"📍 {html.escape(a['region'])}\n"
            f"{badge} ({len(c['publishers'])} outlet{'s' if len(c['publishers'])>1 else ''})\n"
            f"🔗 Sources: {' · '.join(links)}")

def send_telegram(text):
    if DRY_RUN:
        print("---- DRY RUN ----\n" + text + "\n"); return True
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", timeout=30,
                      json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True})
    if not r.ok:
        print("[error] telegram:", r.text)
    return r.ok


# ----------------------------- MAIN -----------------------------------
def main():
    state = load_state()
    recent = [p for p in state["posted"] if p["ts"] > time.time() - DEDUP_HOURS * 3600]

    clusters = cluster(fetch_all())

    # Gate 1: source check (>=2 independent outlets OR one tier-1 outlet)
    clusters = [c for c in clusters if len(c["publishers"]) >= 2 or c["tier1"]]
    # Gate 2: not already posted
    clusters = [c for c in clusters if not any(similar(c["title"], p["title"]) for p in recent)]
    # Most corroborated + newest first
    clusters.sort(key=lambda c: (-len(c["publishers"]), -c["latest"]))
    print(f"[info] {len(clusters)} candidate stories after source/dedup gates")

    posted = calls = 0
    for c in clusters:
        if posted >= MAX_POSTS_PER_RUN or calls >= MAX_LLM_CALLS:
            break
        try:
            calls += 1
            a = analyze(c)
            time.sleep(7)                       # stay under free-tier RPM
        except Exception as ex:
            print(f"[warn] analysis failed: {ex}"); continue

        print(f"[check] {c['title'][:70]} -> {a.get('claim_type')} risk={a.get('fake_risk')} "
              f"conf={a.get('confidence')} imp={a.get('importance')}")

        ok = (a.get("is_war_news")
              and a.get("claim_type") in ("confirmed", "attributed")
              and a.get("fake_risk") == "low"
              and not a.get("contradiction")
              and a.get("confidence", 0) >= MIN_CONFIDENCE
              and a.get("importance", 0) >= MIN_IMPORTANCE)
        # a single-party claim is only posted if 2+ outlets carry it
        if a.get("claim_type") == "attributed" and len(c["publishers"]) < 2:
            ok = False
        if not ok:
            continue

        if send_telegram(format_msg(c, a)):
            state["posted"].append({"title": c["title"], "ts": time.time()})
            posted += 1
            time.sleep(3)

    save_state(state)
    print(f"[done] posted {posted}, LLM calls {calls}")

if __name__ == "__main__":
    main()
