#!/usr/bin/env python3
"""
bot.py — récupère les cotes Betby (props kills LoL / Dota 2) en boucle,
garde l'historique, repère les nouvelles lignes et les mouvements, et peut prévenir sur Telegram.

Lancement :
  python bot.py                 # boucle toutes les 10 min
  python bot.py --once          # un seul passage (pour GitHub Actions / tâche planifiée)
Telegram (optionnel) : variables d'environnement TELEGRAM_TOKEN et TELEGRAM_CHAT_ID
Fichiers produits : data/latest.json (dernières cotes), data/history.csv (tout l'historique), data/state.json
"""
import argparse, asyncio, csv, json, os, sys, time, datetime as dt
import requests
import betby_scraper
from betby_scraper import scrape
from compare import load_pp, parse_pp, compare, fmt

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA, exist_ok=True)
TG_TOKEN, TG_CHAT = os.getenv("TELEGRAM_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")

def key(p): return f"{p['event_id']}|{p['player']}|{p['map']}|{p['market_id']}"

def tg(msg: str):
    if not (TG_TOKEN and TG_CHAT): return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
    except Exception as e: print("Telegram KO:", e, file=sys.stderr)

SB_URL, SB_KEY = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
def push_supabase(alerts: list[dict]):
    """Envoie les bets EV dans la table ev_alerts (affichés dans l'app esport). Un même bet n'est jamais remis à « new »."""
    if not (SB_URL and SB_KEY and alerts): return
    cols = ["key", "player", "game", "stat", "side", "map", "min_under", "pp_line", "pp_maps", "pp_per_map", "book_line", "book_odds", "edge", "match", "tournament", "tier", "start"]
    rows = [{c: a.get(c) for c in cols} for a in alerts]
    try:
        r = requests.post(f"{SB_URL}/rest/v1/ev_alerts?on_conflict=key", json=rows, timeout=20,
                          headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json", "Prefer": "resolution=ignore-duplicates,return=minimal"})
        if r.status_code >= 300: print("Supabase KO :", r.status_code, r.text[:200], file=sys.stderr)
    except Exception as e: print("Supabase KO :", e, file=sys.stderr)

def run_once(limit: int, wait: float, headful: bool):
    n_alerts = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    props = asyncio.run(scrape(limit=limit, wait=wait, headful=headful))
    state_f = os.path.join(DATA, "state.json")
    state = json.load(open(state_f, encoding="utf-8")) if os.path.exists(state_f) else {}
    new, moved = [], []
    for p in props:
        k = key(p); old = state.get(k)
        if old is None: new.append(p)
        elif old["line"] != p["line"] or abs(old["odds_under"] - p["odds_under"]) >= 0.05: moved.append((old, p))
        state[k] = {"line": p["line"], "odds_over": p["odds_over"], "odds_under": p["odds_under"], "seen": now}
    json.dump(state, open(state_f, "w", encoding="utf-8"))
    json.dump({"at": now, "props": props}, open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    hist = os.path.join(DATA, "history.csv"); first = not os.path.exists(hist)
    with open(hist, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if first: w.writerow(["at", "sport", "tournament", "match", "player", "map", "line", "odds_over", "odds_under", "event_id", "market_id"])
        for p in props: w.writerow([now, p["sport"], p["tournament"], p["match"], p["player"], p["map"], p["line"], p["odds_over"], p["odds_under"], p["event_id"], p["market_id"]])
    print(f"{now} · {len(props)} props · {len(new)} nouvelles · {len(moved)} mouvements")
    # Notifications : on regroupe pour ne pas spammer
    if new and os.getenv("BETBY_NOTIFY") == "1":
        lines = [f"• <b>{p['player']}</b> ({p['match']}) map {p['map']} — {p['line']} kills · O {p['odds_over']} / U {p['odds_under']}" for p in new[:25]]
        tg(f"🆕 {len(new)} nouvelles lignes Betby\n" + "\n".join(lines) + ("\n…" if len(new) > 25 else ""))
    # PrizePicks (Apify) → écarts
    if os.getenv("APIFY_TOKEN") and os.getenv("APIFY_ACTOR"):
        try:
            raw = load_pp(); pp = parse_pp(raw)
            print(f"   PrizePicks : {len(raw)} lignes reçues d'Apify · {len(pp)} lignes esport utilisables")
            alerts = compare(props, pp)
            sent_f = os.path.join(DATA, "sent.json"); sent = json.load(open(sent_f, encoding="utf-8")) if os.path.exists(sent_f) else {}
            push_supabase(alerts)
            fresh = [x for x in alerts if x["key"] not in sent]
            for x in fresh[:15]: tg("🎯 " + fmt(x)); sent[x["key"]] = now
            json.dump(sent, open(sent_f, "w", encoding="utf-8"))
            json.dump(alerts, open(os.path.join(DATA, "alerts.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            n_alerts = len(alerts)
            print(f"   PrizePicks : {len(alerts)} écarts ≥ seuil · {len(fresh)} nouveaux envoyés")
        except Exception as e: print("PrizePicks KO :", e, file=sys.stderr)
    if moved and os.getenv("BETBY_NOTIFY") == "1":
        lines = [f"• <b>{n['player']}</b> map {n['map']} : {o['line']} → {n['line']} · U {o['odds_under']} → {n['odds_under']}" for o, n in moved[:25]]
        tg(f"📉 {len(moved)} lignes ont bougé\n" + "\n".join(lines))
    return n_alerts

J = {"cs2": "CS2", "lol": "League of Legends", "dota": "Dota 2", "valo": "Valorant"}
def set_games(codes: str):
    jeux = [J[j.strip().lower()] for j in codes.split(",") if j.strip().lower() in J]
    if not jeux: raise ValueError("jeux : choisis parmi cs2,lol,dota,valo")
    betby_scraper.WANTED.clear(); betby_scraper.WANTED.update(jeux)
    os.environ["APIFY_LEAGUES"] = ",".join(jeux)
    print("Jeux :", ", ".join(jeux))

def sb(path, method="GET", body=None):
    r = requests.request(method, f"{SB_URL}/rest/v1/{path}", json=body, timeout=20,
                         headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json", "Prefer": "return=minimal"})
    r.raise_for_status(); return r.json() if method == "GET" else None

def ecoute(a):
    """Attend les runs demandés depuis l'app (table ev_runs) — ne coûte rien tant que tu ne cliques pas."""
    if not (SB_URL and SB_KEY): sys.exit("SUPABASE_URL / SUPABASE_KEY manquants")
    print("En écoute : clique « Lancer un run » dans l'app (Suivi → Bets potentiels). Ctrl+C pour arrêter.")
    while True:
        try:
            todo = sb("ev_runs?status=eq.pending&order=id.asc&limit=1")
            if todo:
                job = todo[0]; rid = job["id"]
                sb(f"ev_runs?id=eq.{rid}", "PATCH", {"status": "running"})
                try:
                    set_games(job.get("games") or "cs2,lol,dota")
                    n = run_once(a.limit, a.wait, a.headful)
                    sb(f"ev_runs?id=eq.{rid}", "PATCH", {"status": "done", "n_alerts": n, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                except Exception as e:
                    print("Run KO :", e, file=sys.stderr)
                    sb(f"ev_runs?id=eq.{rid}", "PATCH", {"status": "error", "error": str(e)[:300], "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        except Exception as e: print("Écoute KO :", e, file=sys.stderr)
        time.sleep(10)

# ── Mode « cycle » (GitHub Actions, sans ton PC) ─────────────────────────────
# Chaque passage : Betby rafraîchi + comparé à la DERNIÈRE recherche Apify (gardée).
# Un nouvel Apify n'est lancé que si tu cliques « Lancer un run » dans l'app.
OFF = dt.timezone(dt.timedelta(hours=float(os.getenv("BOT_UTC_OFFSET", "7"))))
def hh(t): return t.astimezone(OFF).strftime("%H:%M") if t else "?"

def apify_last_time():
    tok, actor = os.getenv("APIFY_TOKEN"), os.getenv("APIFY_ACTOR")
    try:
        d = requests.get(f"https://api.apify.com/v2/acts/{actor}/runs/last?token={tok}", timeout=20).json().get("data") or {}
        t = d.get("finishedAt") or d.get("startedAt")
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None
    except Exception: return None

def claim_run():
    """Prend la demande « Lancer un run » la plus ancienne (une seule machine la prend)."""
    todo = sb("ev_runs?status=eq.pending&order=id.asc&limit=1")
    if not todo: return None
    r = requests.patch(f"{SB_URL}/rest/v1/ev_runs?id=eq.{todo[0]['id']}&status=eq.pending", json={"status": "running"}, timeout=20,
                       headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json", "Prefer": "return=representation"})
    got = r.json() if r.ok else []
    return got[0] if got else None

def cycle(a):
    now = dt.datetime.now(dt.timezone.utc)
    job = claim_run() if (SB_URL and SB_KEY) else None
    try:
        set_games(job.get("games") if job and job.get("games") else os.getenv("BOT_JEUX", a.jeux))
        os.environ["APIFY_RUN"] = "1" if job else "0"          # sinon : on relit la dernière recherche Apify
        props = asyncio.run(scrape(limit=a.limit, wait=a.wait, headful=False))
        raw = load_pp(); pp = parse_pp(raw); alerts = compare(props, pp)
        apify_at = now if job else apify_last_time()
        print(f"{hh(now)} · Betby {len(props)} props · Apify {len(raw)} lignes ({hh(apify_at)}) · {len(alerts)} bets")
        # Ce qui existe déjà dans l'app (2 derniers jours)
        since = (now - dt.timedelta(days=2)).isoformat()
        old = {r["key"]: r["status"] for r in sb(f"ev_alerts?select=key,status&created_at=gte.{since}")}
        cols = ["key", "player", "game", "stat", "side", "map", "min_under", "pp_line", "pp_maps", "pp_per_map", "book_line", "book_odds", "edge", "match", "tournament", "tier", "start"]
        fresh = [x for x in alerts if x["key"] not in old]
        back = [x for x in alerts if old.get(x["key"]) == "expired"]
        if fresh:
            requests.post(f"{SB_URL}/rest/v1/ev_alerts?on_conflict=key", json=[{c: x.get(c) for c in cols} for x in fresh], timeout=20,
                          headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json", "Prefer": "resolution=ignore-duplicates,return=minimal"})
        for x in back: sb("ev_alerts?key=eq." + requests.utils.quote(x["key"], safe=""), "PATCH", {"status": "new"})
        # Les bets qui ne passent plus (ligne Betby qui a bougé, cote tombée…) disparaissent de l'app
        cur = {x["key"] for x in alerts}
        for k, st in old.items():
            if st == "new" and k not in cur: sb("ev_alerts?key=eq." + requests.utils.quote(k, safe=""), "PATCH", {"status": "expired"})
        foot = f"\n🕐 Betby {hh(now)} · Apify {hh(apify_at)}"
        for x in (fresh + back)[:15]: tg("🎯 " + fmt(x) + foot)
        if job: tg(f"✅ Run terminé · {len(alerts)} bet(s) · {len(fresh)} nouveau(x){foot}")
        sb("bot_status?id=eq.1", "PATCH", {"betby_at": now.isoformat(), "apify_at": apify_at.isoformat() if apify_at else None, "n_props": len(props), "n_pp": len(raw), "n_alerts": len(alerts)})
        if job: sb(f"ev_runs?id=eq.{job['id']}", "PATCH", {"status": "done", "n_alerts": len(alerts), "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    except Exception as e:
        print("Cycle KO :", e, file=sys.stderr)
        if job: sb(f"ev_runs?id=eq.{job['id']}", "PATCH", {"status": "error", "error": str(e)[:300], "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        raise

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true"); ap.add_argument("--every", type=int, default=10, help="minutes entre deux passages")
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--wait", type=float, default=8); ap.add_argument("--headful", action="store_true")
    ap.add_argument("--jeux", default="cs2,lol,dota", help="jeux à scanner : cs2,lol,dota,valo")
    ap.add_argument("--ecoute", action="store_true", help="attend les runs lancés depuis l'app")
    ap.add_argument("--cycle", action="store_true", help="un passage Betby + dernière recherche Apify (GitHub Actions)")
    a = ap.parse_args()
    if a.cycle: cycle(a); sys.exit(0)
    if a.ecoute: ecoute(a)
    set_games(a.jeux)
    while True:
        try: run_once(a.limit, a.wait, a.headful)
        except Exception as e: print("Erreur passage :", e, file=sys.stderr)
        if a.once: break
        time.sleep(a.every * 60)
