#!/usr/bin/env python3
"""
betby_scraper.py — props joueurs esport (CS2, LoL, Dota 2, Valorant) sur la démo publique Betby.

Comment ça marche (d'après les vraies données Betby) :
  1. /v4/prematch/brand/{BRAND}/en-GB/0 donne la liste des « paquets » d'événements → on les lit tous.
  2. On garde les matchs esport qui ont  desc.player_props = true  (les autres n'ont pas de props joueurs).
  3. Pour chacun : /v4/prematch/brand/{BRAND}/event/en-GB/{id}  (marchés du match)
                   /v3/descriptions/brand/{BRAND}/event/{id}/en-GB  (noms des joueurs)
     Si aucun prop n'est trouvé ainsi, on ouvre la page avec Playwright et on écoute le réseau (secours).
  4. Les noms de marchés viennent de /v3/descriptions/brand/{BRAND}/markets/en-GB
     ex. 60040 = « {!mapnr} map - {%player} total kills »  → issues 12 = over, 13 = under.

Usage :
  python betby_scraper.py --discover --limit 3     # + fichiers bruts dans ./betby_dump
  python betby_scraper.py --out props.json
"""
import argparse, asyncio, json, os, re, sys
from typing import Any
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

API = "https://demoapi.betby.com/api"
SITE = "https://demo.betby.com"
BRAND = "1653815133341880320"
LANG = "en-GB"
SPORTS = {"109": "CS2", "110": "League of Legends", "111": "Dota 2", "194": "Valorant"}
WANTED = {s.strip() for s in os.getenv("BETBY_SPORTS", "CS2,Dota 2,League of Legends,Valorant").split(",")}
STATS = [("headshot", "headshots"), ("kill", "kills"), ("assist", "assists"), ("death", "deaths")]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
S = requests.Session(); S.headers.update({"User-Agent": UA, "Accept": "application/json", "Origin": SITE, "Referer": SITE + "/"})

def log(*a): print("[betby]", *a, file=sys.stderr, flush=True)
def get(path: str) -> Any:
    import time
    for i in range(4):                     # 503 / 429 passagers : on réessaie (5 s, 15 s, 30 s)
        r = S.get(API + path, timeout=25)
        if r.status_code not in (429, 502, 503, 504) or i == 3: break
        log(f"Betby {r.status_code}, nouvel essai…"); time.sleep((5, 15, 30)[i])
    r.raise_for_status(); return r.json()

# ── Noms des marchés ─────────────────────────────────────────────────────────
_MK: dict = {}
def market_names() -> dict:
    global _MK
    if not _MK:
        d = get(f"/v3/descriptions/brand/{BRAND}/markets/{LANG}")
        _MK = d.get("markets", d) if isinstance(d, dict) else {}
    return _MK

def stat_of(name: str):
    n = (name or "").lower()
    if "{%player}" not in n: return None
    for frag, st in STATS:
        if frag in n: return st
    return None

# ── 1-2) Événements esport avec props joueurs ───────────────────────────────
def fetch_events() -> list[dict]:
    tours, raw, kind_of, inline = {}, {}, {}, {}
    for kind in ("prematch", "live"):   # avant-match + en direct (pour les lignes Map 3)
        try: root = get(f"/v4/{kind}/brand/{BRAND}/{LANG}/0")
        except Exception as e: log(kind, "KO", e); continue
        versions = list(root.get("top_events_versions") or []) + list(root.get("rest_events_versions") or [])
        if not versions and root.get("version"): versions = [root["version"]]
        for v in versions:
            try: chunk = get(f"/v4/{kind}/brand/{BRAND}/{LANG}/{v}")
            except Exception as e: log("paquet KO", v, e); continue
            tours.update(chunk.get("tournaments") or {})
            for eid, e in (chunk.get("events") or {}).items():
                d = (e or {}).get("desc") or {}
                sid = str(d.get("sport") or "")
                if sid in SPORTS and SPORTS[sid] in WANTED:
                    raw[eid] = d; kind_of[eid] = kind; inline[eid] = (e or {}).get("markets") or {}
    events = []
    for eid, d in raw.items():
        t = tours.get(str(d.get("tournament"))) or {}
        events.append({"id": eid, "kind": kind_of.get(eid, "prematch"), "inline": inline.get(eid) or {}, "sport_id": str(d["sport"]), "sport": SPORTS[str(d["sport"])], "player_props": bool(d.get("player_props")),
                       "tournament": t.get("name", ""), "tier": t.get("tier"),
                       "match": " vs ".join(c.get("name", "") for c in d.get("competitors") or []), "start": d.get("scheduled")})
    log(f"{len(events)} matchs esport ({sum(e['kind'] == 'live' for e in events)} en direct) · {sum(e['player_props'] for e in events)} avec props joueurs")
    return [e for e in events if e["player_props"] or e["kind"] == "live"]

# ── 3) Joueurs + marchés d'un match ─────────────────────────────────────────
def players_of(raw: Any) -> dict:
    out = {}
    def add(pid, name):
        if pid is None or not isinstance(name, str) or not name: return
        pid = str(pid); out[pid] = name; out[pid.split(":")[-1]] = name
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("name"), str) and o.get("id") is not None: add(o["id"], o["name"])
            for k, v in o.items():
                if re.fullmatch(r"(sr:player:)?\d+", str(k)):
                    if isinstance(v, dict): add(k, v.get("name"))
                    elif isinstance(v, str): add(k, v)
                walk(v)
        elif isinstance(o, list):
            for x in o: walk(x)
    walk(raw); return out

def extract(markets: dict, players: dict, ev: dict) -> list[dict]:
    names = market_names(); out = []
    for mid, specs in (markets or {}).items():
        md = names.get(str(mid)) or {}
        st = stat_of(md.get("name", ""))
        if not st or not isinstance(specs, dict): continue
        for spec, outs in specs.items():
            sp = dict(re.findall(r"(?:^|\|)(\w+)=([^|]+)", spec or ""))
            if "player" not in sp or "total" not in sp or not isinstance(outs, dict): continue
            def k(o):
                x = (outs.get(o) or {}).get("k")
                try: return float(x)
                except (TypeError, ValueError): return None
            over, under = k("12"), k("13")
            if not over or not under: continue
            pid = sp["player"]; pname = players.get(pid) or players.get(pid.split(":")[-1]) or pid
            out.append({"player": pname, "stat": st, "map": sp.get("mapnr", "série"), "line": float(sp["total"]),
                        "odds_over": over, "odds_under": under, "sport": ev["sport"], "tournament": ev["tournament"], "tier": ev.get("tier"),
                        "match": ev["match"], "source": "betby", "market_id": str(mid),
                        "market_name": md.get("name", "").replace("{%player}", pname).replace("{!mapnr}", sp.get("mapnr", "")).replace("{mapnr}", sp.get("mapnr", "")),
                        "event_id": ev["id"], "start": ev.get("start"), "live": ev.get("kind") == "live"})
    return out

def event_rest(ev: dict):
    det = None
    for kind in ([ev.get("kind", "prematch")] + (["prematch"] if ev.get("kind") == "live" else [])):
        try: det = get(f"/v4/{kind}/brand/{BRAND}/event/{LANG}/{ev['id']}"); break
        except Exception: det = None
    e = ((det or {}).get("events") or {}).get(ev["id"]) or {"markets": ev.get("inline") or {}}
    try: desc = get(f"/v3/descriptions/brand/{BRAND}/event/{ev['id']}/{LANG}")
    except Exception: desc = None
    return e.get("markets") or {}, players_of(desc), det, desc

async def event_browser(ev: dict, wait_s: float, headful: bool):
    """Secours : ouvre la page du match et récupère uniquement ce qui concerne CE match."""
    from playwright.async_api import async_playwright
    blobs, markets, players = [], {}, {}
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=not headful, args=["--no-sandbox"])
        page = await (await br.new_context(user_agent=UA, locale="en-GB")).new_page()
        async def grab(resp):
            if "betby.com" not in resp.url: return
            try: blobs.append({"url": resp.url, "json": await resp.json()})
            except Exception: pass
        page.on("response", lambda r: asyncio.ensure_future(grab(r)))
        def on_ws(ws):
            def fr(p):
                try:
                    t = p if isinstance(p, str) else p.decode("utf-8", "ignore")
                    i = min([x for x in (t.find("{"), t.find("[")) if x >= 0], default=-1)
                    if i >= 0: blobs.append({"url": ws.url, "json": json.loads(t[i:])})
                except Exception: pass
            ws.on("framereceived", fr)
        page.on("websocket", on_ws)
        await page.goto(f"{SITE}/sportsbook/classic/event/{ev['id']}", wait_until="domcontentloaded", timeout=45000)
        for label in ["Player props", "Players", "Player", "Kills"]:
            try:
                loc = page.get_by_text(label, exact=False).first
                if await loc.count(): await loc.click(timeout=2000); await page.wait_for_timeout(1500)
            except Exception: pass
        await page.wait_for_timeout(int(wait_s * 1000))
        await br.close()
    def walk(o):
        if isinstance(o, dict):
            x = o.get(ev["id"])
            if isinstance(x, dict) and isinstance(x.get("markets"), dict):
                for mid, specs in x["markets"].items(): markets.setdefault(mid, {}).update(specs or {})
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    for b in blobs:
        walk(b["json"])
        if "descriptions" in b["url"] and ev["id"] in b["url"]: players.update(players_of(b["json"]))
    return markets, players, blobs

# ── Utilisable depuis bot.py ────────────────────────────────────────────────
async def scrape(limit: int = 0, wait: float = 6, headful: bool = False, discover: bool = False) -> list[dict]:
    market_names()
    events = fetch_events()
    if limit: events = events[:limit]
    if discover: os.makedirs("betby_dump", exist_ok=True)
    props: list[dict] = []
    for i, ev in enumerate(events, 1):
        det = desc = None; blobs = []
        try: mk, pl, det, desc = event_rest(ev)
        except Exception as e: log("REST KO", ev["id"], e); mk, pl = {}, {}
        got = extract(mk, pl, ev); how = "REST"
        if not got and ev["player_props"]:
            try:
                mk2, pl2, blobs = await event_browser(ev, wait, headful)
                got = extract(mk2, {**pl, **pl2}, ev); how = "navigateur"
            except Exception as e: log("navigateur KO", ev["id"], e)
        props += got
        log(f"{i}/{len(events)} {ev['sport']} · {ev['match']} ({ev['tournament']}) → {len(got)} props [{how}]")
        if discover:
            with open(f"betby_dump/{ev['id']}.json", "w", encoding="utf-8") as f:
                json.dump({"event": ev, "detail": det, "descriptions": desc, "props": got[:60],
                           "captured_urls": [b["url"] for b in blobs]}, f, ensure_ascii=False, indent=1)
    return props

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--wait", type=float, default=6)
    ap.add_argument("--out", default=""); ap.add_argument("--discover", action="store_true"); ap.add_argument("--headful", action="store_true")
    a = ap.parse_args()
    props = await scrape(a.limit, a.wait, a.headful, a.discover)
    js = json.dumps(props, ensure_ascii=False, indent=1)
    if a.out: open(a.out, "w", encoding="utf-8").write(js); log(f"{len(props)} props → {a.out}")
    else: print(js)

if __name__ == "__main__":
    asyncio.run(main())
