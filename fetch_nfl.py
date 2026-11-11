"""
NFL weekly player data for the Floor Dots NFL screener.

Pulls three free nflverse files (weekly stats, snap counts, play-by-play),
counts red zone targets and carries, joins snap share, and writes:
    data/nfl_weekly_<season>.csv
    data/latest.json   (tells the screener which file to load)

Runs on GitHub Actions; also works anywhere with Python 3. Prop prices need `pip install propline`
and a PROPLINE_KEY environment variable; without them the rest still runs.
Usage:  python fetch_nfl.py          # current season
        python fetch_nfl.py 2025     # a past season
"""

import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import unicodedata
import urllib.request
from collections import defaultdict

today = dt.date.today()
SEASON = int(sys.argv[1]) if len(sys.argv) > 1 else (today.year if today.month >= 3 else today.year - 1)
BASE = "https://github.com/nflverse/nflverse-data/releases/download"
URLS = {
    "stats": f"{BASE}/stats_player/stats_player_week_{SEASON}.csv",
    "snaps": f"{BASE}/snap_counts/snap_counts_{SEASON}.csv",
    "pbp": f"{BASE}/pbp/play_by_play_{SEASON}.csv.gz",
}
POSITIONS = {"QB", "RB", "WR", "TE"}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

FIELDS = ["player_id", "player", "position", "team", "opponent", "week", "game_id",
          "completions", "attempts", "passing_yards", "passing_tds", "interceptions",
          "carries", "rushing_yards", "rushing_tds",
          "targets", "receptions", "receiving_yards", "receiving_tds", "target_share",
          "rz_targets", "rz_carries", "snaps", "snap_pct",
          "total_tds", "rush_rec_yds"]


def download(url):
    print(f"Downloading {url.rsplit('/', 1)[-1]} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "floor-dots-nfl"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    if url.endswith(".gz"):
        raw = gzip.decompress(raw)
    return csv.DictReader(io.StringIO(raw.decode("utf-8")))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def name_key(name):
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    n = re.sub(r"[^a-z ]", "", n.lower())
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return " ".join(n.split())


# ---------------------------------------------------------------- prop prices (PropLine)
PROP_MARKETS = {"player_anytime_td": ("total_tds", 1), "player_2plus_td": ("total_tds", 2), "player_receptions": ("receptions", None),
                "player_reception_yds": ("receiving_yards", None), "player_rush_yds": ("rushing_yards", None),
                "player_rush_attempts": ("carries", None), "player_rush_reception_yds": ("rush_rec_yds", None),
                "player_pass_yds": ("passing_yards", None), "player_pass_tds": ("passing_tds", None)}


def implied(a):
    a = float(a)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def fetch_props():
    """Prices for games in the next 4 days from Novig, DraftKings and FanDuel. Needs the PROPLINE_KEY secret."""
    key = os.environ.get("PROPLINE_KEY")
    if not key:
        return 0
    from propline import PropLine
    client = PropLine(key)
    now = dt.datetime.now(dt.timezone.utc)
    evs = client.get_events("football_nfl")
    evs = evs.get("data", evs) if isinstance(evs, dict) else evs
    rows = []
    for e in evs or []:
        try:
            t = dt.datetime.fromisoformat(str(e.get("commence_time")).replace("Z", "+00:00"))
        except Exception:
            continue
        if not (now - dt.timedelta(hours=3) <= t <= now + dt.timedelta(days=4)):
            continue
        odds = client.get_odds("football_nfl", event_id=e["id"], markets=list(PROP_MARKETS), bookmakers=["novig", "draftkings", "fanduel"])
        games = odds.get("data", odds) if isinstance(odds, dict) else odds
        for g in (games if isinstance(games, list) else [games]):
            for b in g.get("bookmakers", []):
                for m in b.get("markets", []):
                    stat, fixed = PROP_MARKETS.get(m.get("key"), (None, None))
                    if not stat:
                        continue
                    outs = m.get("outcomes", [])
                    for o in outs:
                        price, name = o.get("price"), str(o.get("name", ""))
                        player = o.get("description") or name
                        if price is None or not player:
                            continue
                        fair = None
                        if name == "Under":
                            continue
                        if name == "Over":
                            if o.get("point") is None:
                                continue
                            at_least = int(float(o["point"]) + 0.5)      # over 0.5 TDs = 1 or more
                            under = next((u for u in outs if u.get("name") == "Under" and u.get("description") == player
                                          and u.get("point") == o.get("point")), None)
                            p = implied(price)
                            if under and under.get("price") is not None:
                                fair = p / (p + implied(under["price"]))
                        elif fixed:                                      # yes/no TD markets list the player as the outcome
                            at_least, p = fixed, implied(price)
                        else:
                            mt = re.match(r"\s*(\d+)\+", name)         # ladders like '60+ Yards'
                            if not mt:
                                continue
                            at_least, p = int(mt.group(1)), implied(price)
                        rows.append({"player": player, "stat": stat, "at_least": at_least, "book": b.get("key"), "price": price,
                                     "prob": round(p, 3), "fair": None if fair is None else round(fair, 3), "liquidity": o.get("liquidity"),
                                     "game": f"{g.get('away_team', '')} at {g.get('home_team', '')}", "kickoff": e.get("commence_time")})
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "props.json"), "w") as f:
        json.dump({"updated": now.isoformat(timespec="seconds"), "props": rows}, f, separators=(",", ":"))
    return len(rows)


def main():
    # Red zone targets and carries from play-by-play
    rz_t, rz_c = defaultdict(int), defaultdict(int)
    for p in download(URLS["pbp"]):
        if p.get("season_type") != "REG":
            continue
        yl = num(p.get("yardline_100"))
        if not (0 < yl <= 20):
            continue
        wk = p.get("week")
        if p.get("pass_attempt") == "1" and p.get("receiver_player_id"):
            rz_t[(p["receiver_player_id"], wk)] += 1
        if p.get("rush_attempt") == "1" and p.get("rusher_player_id"):
            rz_c[(p["rusher_player_id"], wk)] += 1

    # Offensive snaps, keyed by name + team + week
    snaps, by_last = {}, defaultdict(list)
    for s in download(URLS["snaps"]):
        if s.get("game_type") != "REG":
            continue
        val = (num(s["offense_snaps"]), num(s["offense_pct"]) * 100)
        key = name_key(s["player"])
        snaps[(key, s["team"], s["week"])] = val
        if key:
            by_last[(key.split()[-1], s["team"], s["week"])].append(val)

    rows, matched = [], 0
    for r in download(URLS["stats"]):
        if r.get("season_type") != "REG" or r.get("position") not in POSITIONS:
            continue
        pid, wk = r["player_id"], r["week"]
        name = r.get("player_display_name") or r.get("player_name")
        key = name_key(name)
        sn = snaps.get((key, r["team"], wk))
        if not sn and key:  # nickname fallback, e.g. Bam vs Zonovan
            cands = by_last.get((key.split()[-1], r["team"], wk), [])
            sn = cands[0] if len(cands) == 1 else None
        if sn:
            matched += 1
        out = {
            "player_id": pid, "player": name, "position": r["position"],
            "team": r["team"], "opponent": r.get("opponent_team", ""),
            "week": int(wk), "game_id": r.get("game_id", ""),
            "completions": num(r["completions"]), "attempts": num(r["attempts"]),
            "passing_yards": num(r["passing_yards"]), "passing_tds": num(r["passing_tds"]),
            "interceptions": num(r.get("passing_interceptions")),
            "carries": num(r["carries"]), "rushing_yards": num(r["rushing_yards"]),
            "rushing_tds": num(r["rushing_tds"]),
            "targets": num(r["targets"]), "receptions": num(r["receptions"]),
            "receiving_yards": num(r["receiving_yards"]), "receiving_tds": num(r["receiving_tds"]),
            "target_share": round(num(r.get("target_share")) * 100, 1),
            "rz_targets": rz_t.get((pid, wk), 0), "rz_carries": rz_c.get((pid, wk), 0),
            "snaps": sn[0] if sn else "", "snap_pct": round(sn[1], 1) if sn else "",
        }
        out["total_tds"] = out["rushing_tds"] + out["receiving_tds"]
        out["rush_rec_yds"] = out["rushing_yards"] + out["receiving_yards"]
        rows.append(out)

    if not rows:
        sys.exit("No rows found. nflverse may not have published this season yet.")

    # nflverse only lists a player in a week if he recorded a stat. Add zero rows
    # for games he played (offensive snaps) but didn't record anything, so hit
    # rates aren't inflated by silently skipping his empty games.
    n_stat_rows = len(rows)
    known = {}
    have = set()
    for o in rows:
        known[(name_key(o["player"]), o["team"])] = o
        have.add((o["player_id"], o["week"]))
    zero_fields = [f for f in FIELDS if f not in ("player_id", "player", "position", "team",
                                                  "opponent", "week", "game_id", "snaps", "snap_pct")]
    added = 0
    for s in download(URLS["snaps"]):
        if s.get("game_type") != "REG" or num(s["offense_snaps"]) <= 0:
            continue
        base = known.get((name_key(s["player"]), s["team"]))
        if not base or (base["player_id"], int(s["week"])) in have:
            continue
        z = {f: 0 for f in zero_fields}
        z.update({"player_id": base["player_id"], "player": base["player"], "position": base["position"],
                  "team": s["team"], "opponent": s.get("opponent", ""), "week": int(s["week"]),
                  "game_id": s.get("game_id", ""), "snaps": num(s["offense_snaps"]),
                  "snap_pct": round(num(s["offense_pct"]) * 100, 1)})
        rows.append(z)
        have.add((base["player_id"], z["week"]))
        added += 1
    print(f"Added {added} games where a player was on the field but recorded no stats")

    rows.sort(key=lambda x: (x["player"], x["week"]))
    os.makedirs(OUT_DIR, exist_ok=True)
    fname = f"nfl_weekly_{SEASON}.csv"
    with open(os.path.join(OUT_DIR, fname), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    max_week = max(r["week"] for r in rows)
    try:
        n = fetch_props()
        print(f"Props: {n} prices saved")
    except Exception as e:
        print(f"Couldn't load prop prices: {e}")
    with open(os.path.join(OUT_DIR, "latest.json"), "w") as f:
        json.dump({"season": SEASON, "file": fname, "through_week": max_week,
                   "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}, f)

    print(f"Saved {len(rows)} player-games through week {max_week} to data/{fname}")
    print(f"Snap counts matched for {matched / n_stat_rows:.0%} of stat rows")


if __name__ == "__main__":
    main()
