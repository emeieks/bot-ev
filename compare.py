#!/usr/bin/env python3
"""
compare.py — PrizePicks (via Apify) ↔ Betby : calcule ton écart et prépare les alertes.

PrizePicks : on lit le résultat de ton acteur Apify (même format que ton fichier : player_name, stat, line, odds_tier…).
  Variables d'environnement :
    APIFY_TOKEN   = ton token Apify (Settings → API & Integrations)
    APIFY_ACTOR   = l'identifiant de l'acteur, ex.  "username~prizepicks-scraper"
    APIFY_RUN     = "1" pour lancer l'acteur à chaque passage (coûte des crédits) ; sinon on lit son DERNIER résultat
  Ou en local : --pp fichier.json

Règles (modifiables) :
  - lignes « standard » seulement (goblin / demon : pas de Under possible), pas de combos ;
  - écart minimum par jeu (Under) : CS2 1.5 · Dota 2 1.75 · Valorant 1.75 · LoL 1.0  (variables MIN_EDGE_CS2…)
"""
import argparse, json, math, os, re, sys, unicodedata
import requests

try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

GAME = {"lol": "League of Legends", "league of legends": "League of Legends", "cs2": "CS2", "csgo": "CS2", "cs": "CS2", "counter-strike": "CS2",
        "dota2": "Dota 2", "dota 2": "Dota 2", "dota": "Dota 2", "val": "Valorant", "valorant": "Valorant"}
# ── TES RÈGLES ── (jeu, maps PrizePicks) → paliers (écart minimum, cote Betby minimum, [ligne PP maximum])
# Un prop est alerté s'il respecte AU MOINS un palier. Un jeu / format absent de la liste = jamais alerté.
RULES = {
    # Map 1+2 (avant match)
    ("CS2", "1+2"): [(2.0, 1.57), (1.25, 1.60)],              # kills ET headshots
    ("Valorant", "1+2"): [(2.0, 1.57), (1.25, 1.60)],
    ("Dota 2", "1+2"): [(2.0, 1.57), (1.25, 1.60), (0.5, 1.60, 8)],   # 0.5 accepté seulement si ligne PP < 8
    ("League of Legends", "1+2"): [(0.75, 1.60)],
    # Map 3 (PrizePicks en direct, ligne unitaire → comparée à la ligne Betby de la map 3)
    ("CS2", "3"): [(1.5, 1.60)],
    ("Valorant", "3"): [(1.5, 1.60)],
    ("Dota 2", "3"): [(1.0, 1.60)],
    ("League of Legends", "3"): [(1.0, 1.60)],
}
SIDES = set(os.getenv("SIDES", "under").lower().split(","))

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)

# ── PrizePicks via Apify ─────────────────────────────────────────────────────
def load_pp(path: str = "") -> list[dict]:
    if path: return json.load(open(path, encoding="utf-8"))
    tok, actor = os.getenv("APIFY_TOKEN"), os.getenv("APIFY_ACTOR")
    if not (tok and actor): print("APIFY_TOKEN / APIFY_ACTOR manquants", file=sys.stderr); return []
    base = f"https://api.apify.com/v2/acts/{actor}"
    if os.getenv("APIFY_RUN") == "1":
        inp = json.load(open("apify_input.json", encoding="utf-8")) if os.path.exists("apify_input.json") else json.loads(os.getenv("APIFY_INPUT", "{}"))
        if os.getenv("APIFY_LEAGUES"): inp["leagues"] = os.getenv("APIFY_LEAGUES").split(",")   # jeux choisis avec --jeux
        print("Apify : leagues =", inp.get("leagues"), file=sys.stderr)
        r = requests.post(f"{base}/run-sync-get-dataset-items?token={tok}", json=inp, timeout=300)
    else:
        r = requests.get(f"{base}/runs/last/dataset/items?token={tok}&clean=1", timeout=60)
    r.raise_for_status(); d = r.json()
    if not d: print("Apify a renvoyé 0 ligne (input vide ? run échoué ? voir l'onglet Runs sur Apify)", file=sys.stderr)
    return d

def parse_pp(items: list[dict]) -> list[dict]:
    out = []
    for p in items:
        if p.get("player_combo") or p.get("event_type") == "combo": continue
        live = bool(p.get("is_live") or p.get("in_game"))
        game = GAME.get(str(p.get("league") or p.get("league_ppid") or "").lower())
        if not game: continue
        st = str(p.get("stat") or "").lower()
        stat = "headshots" if "headshot" in st else "kills" if "kill" in st else None
        if not stat: continue
        m = re.search(r"maps?\s*(\d)(?:\s*[-–+]\s*(\d))?", st) or re.search(r"maps?\s*(\d)(?:\s*[-–+]\s*(\d))?", str(p.get("duration") or "").lower())
        if not m: continue
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        maps = list(range(a, b + 1))
        if live and maps != [3]: continue   # en direct : seulement les lignes Map 3
        wt = str(p.get("allowed_wager_types") or "").lower()
        out.append({"player": p.get("player_name"), "key": norm(p.get("player_name")), "game": game, "stat": stat, "maps": maps,
                    "line": float(p["line"]), "tier": p.get("odds_tier") or "standard",
                    "under_ok": (p.get("odds_tier") in (None, "standard")) and wt in ("", "none", "under", "over,under", "under,over"),
                    "team": p.get("player_team"), "opp": p.get("description"), "start": p.get("start_time"), "projection_id": p.get("projection_id")})
    return out

# ── Comparaison ──────────────────────────────────────────────────────────────
STATS_DBG = {}
def compare(betby: list[dict], pp: list[dict], show_all: bool = False) -> list[dict]:
    book = {}
    for b in betby:
        if str(b.get("map")) not in ("1", "2", "3", "4", "5"): continue
        book.setdefault((norm(b["player"]), b["sport"], b["stat"]), {})[int(b["map"])] = b
    alerts = []; dbg = {"pp_standard": 0, "pp_under_ok": 0, "joueur_trouve": 0, "regle": 0, "compares": []}
    for p in pp:
        if p["tier"] != "standard": continue
        dbg["pp_standard"] += 1
        if p["under_ok"]: dbg["pp_under_ok"] += 1
        maps = book.get((p["key"], p["game"], p["stat"]))
        if not maps: continue
        dbg["joueur_trouve"] += 1
        per = p["line"] / len(p["maps"])                      # ligne PP ramenée par map
        rules = RULES.get((p["game"], "+".join(map(str, p["maps"]))))
        if not rules: continue
        dbg["regle"] += 1
        for m in p["maps"]:                                   # chaque map Betby comparée séparément
            x = maps.get(m)
            if not x: continue
            for side in ("under", "over"):
                if side not in SIDES: continue
                if side == "under" and not p["under_ok"]: continue
                edge = round((x["line"] - per) if side == "under" else (per - x["line"]), 2)
                odds = x["odds_under" if side == "under" else "odds_over"]
                if side == "under": dbg["compares"].append((edge, odds, p["player"], p["game"], p["line"], f"Map {m} {x['line']}", p["under_ok"]))
                ok = [r for r in rules if odds >= r[1] - 1e-9 and (len(r) < 3 or p["line"] < r[2])]
                if not ok: continue
                # Table de référence : Under minimum par map = plus petite ligne en .5 ≥ (PP / maps + écart minimum)
                need = per + min(r[0] for r in ok)
                min_under = math.ceil(need - 0.5 - 1e-9) + 0.5
                if side == "under" and x["line"] < min_under - 1e-9: continue
                if side == "over" and edge < min(r[0] for r in ok) - 1e-9: continue
                alerts.append({"player": x["player"], "game": p["game"], "stat": p["stat"], "side": side, "map": m,
                               "pp_line": p["line"], "pp_maps": "Map " + "+".join(map(str, p["maps"])), "pp_per_map": round(per, 2),
                               "book_line": x["line"], "book_odds": odds, "edge": edge, "min_under": min_under,
                               "match": x["match"], "tournament": x["tournament"], "tier": x.get("tier"), "start": p["start"],
                               "key": f"{p['projection_id']}|{side}|{p['line']}|m{m}|{x['line']}"})
    STATS_DBG.clear(); STATS_DBG.update(dbg)
    return sorted(alerts, key=lambda a: -a["edge"])

def fmt(a: dict) -> str:
    return (f"<b>{a['player']}</b> · {a['game']} · {a['match']}\n"
            f"{a['tournament']}{' (tier ' + a['tier'] + ')' if a.get('tier') else ''}\n"
            f"PP <b>{a['side'].upper()} {a['pp_line']}</b> {a['stat']} ({a['pp_maps']} → {a['pp_per_map']}/map)\n"
            f"Betby Map {a.get('map','?')} : {a['side'].capitalize()} {a['book_line']} @ {a['book_odds']}\n"
            f"📋 Table : min Under {a.get('min_under')}\n"
            f"👉 Écart <b>+{a['edge']}</b>")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--betby", default="props.json"); ap.add_argument("--pp", default=""); ap.add_argument("--tout", action="store_true", help="affiche toutes les comparaisons, même sous tes seuils")
    a = ap.parse_args()
    betby = json.load(open(a.betby, encoding="utf-8")); betby = betby.get("props", betby) if isinstance(betby, dict) else betby
    pp = parse_pp(load_pp(a.pp))
    res = compare(betby, pp)
    print(f"{len(pp)} lignes PrizePicks · {len(betby)} props Betby · {len(res)} alertes", file=sys.stderr)
    for r in res: print(fmt(r).replace("<b>", "").replace("</b>", ""), "\n")
    d = STATS_DBG
    print(f"Détail : {len(pp)} lignes PP esport · {d.get('pp_standard')} standard · {d.get('pp_under_ok')} avec Under permis · "
          f"{d.get('joueur_trouve')} joueurs trouvés sur Betby · {d.get('regle')} avec une règle (format) · {len(res)} passent tes seuils", file=sys.stderr)
    if a.tout:
        print("\nToutes les comparaisons Under (écart · cote Betby · joueur · ligne PP · ligne Betby/map) :")
        for e, o, pl, g, l, bk, ok in sorted(d.get("compares", []), key=lambda x: -x[0]):
            print(f"  {e:+.2f} @ {o:.2f}  {pl} ({g}) PP {l} · Betby {bk}{'' if ok else '  [Under non permis sur PP]'}")
