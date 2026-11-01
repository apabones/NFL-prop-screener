"""
NFL weekly player data for the Floor Dots NFL screener.

Pulls three free nflverse files (weekly stats, snap counts, play-by-play),
counts red zone targets and carries, joins snap share, and writes:
    data/nfl_weekly_<season>.csv
    data/latest.json   (tells the screener which file to load)

Runs on GitHub Actions; also works anywhere with Python 3 (no pip installs).
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

    rows.sort(key=lambda x: (x["player"], x["week"]))
    os.makedirs(OUT_DIR, exist_ok=True)
    fname = f"nfl_weekly_{SEASON}.csv"
    with open(os.path.join(OUT_DIR, fname), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    max_week = max(r["week"] for r in rows)
    with open(os.path.join(OUT_DIR, "latest.json"), "w") as f:
        json.dump({"season": SEASON, "file": fname, "through_week": max_week,
                   "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}, f)

    print(f"Saved {len(rows)} player-games through week {max_week} to data/{fname}")
    print(f"Snap counts matched for {matched / len(rows):.0%} of rows")


if __name__ == "__main__":
    main()
