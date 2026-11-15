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


SCHEDULE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"


def weeks_by_date():
    """Every NFL game date this season mapped to its week (weeks never share a date), for grading picks."""
    try:
        return {r["gameday"]: int(r["week"]) for r in download(SCHEDULE_URL)
                if r.get("season") == str(SEASON) and r.get("gameday") and r.get("week")}
    except Exception as e:
        print(f"Couldn't load the schedule: {e}")
        return {}


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
    rows, weeks = [], weeks_by_date()
    for e in evs or []:
        try:
            t = dt.datetime.fromisoformat(str(e.get("commence_time")).replace("Z", "+00:00"))
        except Exception:
            continue
        if not (now - dt.timedelta(hours=3) <= t <= now + dt.timedelta(days=4)):
            continue
        week = weeks.get((t - dt.timedelta(hours=5)).date().isoformat())   # Eastern calendar day of kickoff
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
                                     "game": f"{g.get('away_team', '')} at {g.get('home_team', '')}", "kickoff": e.get("commence_time"), "week": week})
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "props.json"), "w") as f:
        json.dump({"updated": now.isoformat(timespec="seconds"), "props": rows}, f, separators=(",", ":"))
    return len(rows)


# ---------------------------------------------------------------- Games tab: injuries, weather, red zone
STADIUMS = {  # stadium_id -> (lat, lon)
    "ATL97": (33.755, -84.401), "BAL00": (39.278, -76.623), "BOS00": (42.091, -71.264), "BUF00": (42.774, -78.787),
    "CAR00": (35.226, -80.853), "CHI98": (41.862, -87.617), "CIN00": (39.095, -84.516), "CLE00": (41.506, -81.700),
    "DAL00": (32.748, -97.093), "DEN00": (39.744, -105.020), "DET00": (42.340, -83.046), "GNB00": (44.501, -88.062),
    "HOU00": (29.685, -95.411), "IND00": (39.760, -86.164), "JAX00": (30.324, -81.637), "KAN00": (39.049, -94.484),
    "LAX01": (33.953, -118.339), "MIA00": (25.958, -80.239), "MIN01": (44.974, -93.258), "NAS00": (36.166, -86.771),
    "NOR00": (29.951, -90.081), "NYC01": (40.814, -74.074), "PHI00": (39.901, -75.168), "PHO00": (33.528, -112.263),
    "PIT00": (40.447, -80.016), "SEA00": (47.595, -122.332), "SFO01": (37.403, -121.970), "TAM00": (27.976, -82.503),
    "VEG00": (36.091, -115.184), "WAS00": (38.908, -76.865), "LON00": (51.556, -0.280), "LON02": (51.604, -0.066),
    "MUN01": (48.219, 11.625), "MEX00": (19.303, -99.150), "PAR00": (48.924, 2.360), "MAD01": (40.453, -3.688),
    "RIO00": (-22.912, -43.230), "MEL00": (-37.820, 144.983),
}
RETRACTABLE = {"ATL97", "DAL00", "HOU00", "IND00", "PHO00"}
DOMES = {"DET00", "LAX01", "VEG00", "MIN01", "NOR00", "MUN01", "MEL00", "PAR00", "MAD01"}
TEAM_ABBR = {"Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
             "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
             "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
             "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
             "Los Angeles Rams": "LA", "Los Angeles Chargers": "LAC", "Las Vegas Raiders": "LV", "Miami Dolphins": "MIA",
             "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
             "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "Seattle Seahawks": "SEA",
             "San Francisco 49ers": "SF", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS"}
FIX_ABBR = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX"}
SKILL = {"QB", "RB", "FB", "WR", "TE"}
OLINE = {"C", "G", "T", "OT", "OG", "OL", "LT", "RT", "LG", "RG"}


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
        return json.loads(r.read())


def red_zone(drives):
    """Red-zone trips per game and TD rate for each offense, and what each defense allows, with 1-32 ranks."""
    games = defaultdict(set)
    off, dfn = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for (gid, o, _), (d, rz, res) in drives.items():
        games[o].add(gid)
        if d:
            games[d].add(gid)
        if rz:
            td = res == "Touchdown"
            off[o][0] += 1; off[o][1] += td
            if d:
                dfn[d][0] += 1; dfn[d][1] += td
    out = {}
    for t in games:
        g = len(games[t])
        ot, od = off[t], dfn[t]
        out[t] = {"games": g, "off_trips": round(ot[0] / g, 2), "off_td": round(ot[1] / ot[0], 3) if ot[0] else None,
                  "off_tds": round(ot[1] / g, 2), "def_trips": round(od[0] / g, 2), "def_td": round(od[1] / od[0], 3) if od[0] else None,
                  "def_tds": round(od[1] / g, 2)}

    def rank(key, high_good):
        vals = sorted((v[key] for v in out.values() if v[key] is not None), reverse=high_good)
        for v in out.values():
            v[key + "_rank"] = vals.index(v[key]) + 1 if v[key] is not None else None
    rank("off_trips", True); rank("off_td", True); rank("off_tds", True)
    rank("def_trips", False); rank("def_td", False); rank("def_tds", False)
    return out


def load_injuries():
    """PropLine's injury feed first (same key as prices); ESPN's public feed if that fails."""
    out = defaultdict(list)
    key = os.environ.get("PROPLINE_KEY")
    if key:
        try:
            from propline import PropLine
            data = PropLine(key).get_injuries("football_nfl")
            for p in (data.get("players") if isinstance(data, dict) else data) or []:
                t = FIX_ABBR.get(p.get("team_abbr"), p.get("team_abbr")) or TEAM_ABBR.get(p.get("team", ""))
                if t:
                    out[t].append({"player": p.get("player_name", ""), "pos": p.get("position", ""), "status": p.get("status", ""),
                                   "injury": p.get("injury_type") or "", "since": (p.get("status_since") or "")[:10],
                                   "back": p.get("expected_return") or ""})
            if out:
                return out, "PropLine"
        except Exception as e:
            print(f"PropLine injuries failed, trying ESPN: {e}")
    data = get_json("https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries")
    for team in data.get("injuries", []):
        t = TEAM_ABBR.get(team.get("displayName", ""))
        for i in team.get("injuries", []):
            a = i.get("athlete") or {}
            t2 = t or FIX_ABBR.get((a.get("team") or {}).get("abbreviation"), (a.get("team") or {}).get("abbreviation"))
            if not t2:
                continue
            out[t2].append({"player": a.get("displayName", ""), "pos": (a.get("position") or {}).get("abbreviation", ""),
                            "status": i.get("status", ""), "injury": (i.get("details") or {}).get("type", ""),
                            "since": (i.get("date") or "")[:10], "back": (i.get("details") or {}).get("returnDate", "")})
    return out, "ESPN"


def weather(stadium, gameday, gametime):
    """Hourly forecast at kickoff from Open-Meteo (free, no key). Kickoff times are Eastern."""
    lat, lon = STADIUMS[stadium]
    hh = int((gametime or "13:00").split(":")[0])
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&hourly=temperature_2m,precipitation_probability,"
           "precipitation,wind_speed_10m,wind_gusts_10m&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
           "&timezone=America%%2FNew_York&start_date=%s&end_date=%s") % (lat, lon, gameday, gameday)
    h = get_json(url)["hourly"]
    idx = [i for i, t in enumerate(h["time"]) if hh <= int(t[11:13]) <= hh + 3]   # the ~3 hours of the game
    if not idx:
        return None
    pick = lambda k, f: f(h[k][i] for i in idx if h[k][i] is not None)
    w = {"temp": round(pick("temperature_2m", max)), "rain_chance": pick("precipitation_probability", max),
         "rain_in": round(pick("precipitation", sum), 2), "wind": round(pick("wind_speed_10m", max)), "gusts": round(pick("wind_gusts_10m", max))}
    flags = []
    if (w["rain_chance"] or 0) >= 60 and w["rain_in"] >= 0.15:
        flags.append("heavy rain" if w["rain_in"] >= 0.4 else "rain")
    if w["wind"] >= 15:
        flags.append("wind")
    if w["temp"] <= 25:
        flags.append("cold")
    w["flags"] = flags
    return w


def build_games(drives, rows):
    """Upcoming games for the next 7 days: injuries, weather, and red zone for both sides."""
    today = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).date()
    sched = [g for g in download(SCHEDULE_URL) if g.get("season") == str(SEASON) and g.get("gameday")
             and today <= dt.date.fromisoformat(g["gameday"]) <= today + dt.timedelta(days=7)]
    rz = red_zone(drives)
    # who matters: recent snap share and volume from the screener's own rows
    recent = defaultdict(list)
    for r in rows:
        recent[(name_key(r["player"]), r["team"])].append(r)
    def role(name, team):
        rs = sorted(recent.get((name_key(name), team), []), key=lambda x: x["week"])[-3:]
        if not rs:
            return None
        sp = [r["snap_pct"] for r in rs if r["snap_pct"] != ""]
        return {"snap": round(sum(sp) / len(sp)) if sp else None, "tgt": round(sum(r["targets"] for r in rs) / len(rs), 1),
                "car": round(sum(r["carries"] for r in rs) / len(rs), 1), "att": round(sum(r["attempts"] for r in rs) / len(rs), 1)}
    status = {}
    try:
        inj, src = load_injuries()
        status["injuries"] = f"ok ({src})"
    except Exception as e:
        inj, status["injuries"] = {}, f"failed: {e}"
    out, wx_fail = [], 0
    for g in sched:
        sid, roof = g.get("stadium_id", ""), (g.get("roof") or "").lower()
        if sid in DOMES or roof == "dome":
            wx, venue = None, "dome"
        elif sid in RETRACTABLE or roof in ("closed", "open"):
            wx, venue = None, "retractable roof"
        else:
            venue = "outdoors"
            try:
                wx = weather(sid, g["gameday"], g.get("gametime")) if sid in STADIUMS else None
            except Exception as e:
                wx, wx_fail = None, wx_fail + 1
        teams = {}
        for t in (g["away_team"], g["home_team"]):
            lst = []
            for i in inj.get(t, []):
                st, pos = i["status"].strip(), i["pos"].upper()
                if pos not in SKILL and pos not in OLINE:
                    continue
                if not re.search(r"out|doubt|question|reserve|suspend|pup", st, re.I):
                    continue
                r = role(i["player"], t) if pos in SKILL else None
                key = bool(r and ((r["snap"] or 0) >= 45 or r["tgt"] >= 4 or r["car"] >= 8 or r["att"] >= 15))
                lst.append(dict(i, group="skill" if pos in SKILL else "line", key=key, role=r,
                                long=bool(re.search(r"reserve|pup|suspend", st, re.I))))
            lst.sort(key=lambda x: (x["group"] != "skill", x["long"], not x["key"], 0 if "out" in x["status"].lower() else 1))
            teams[t] = {"injuries": lst, "rz": rz.get(t)}
        out.append({"id": g["game_id"], "week": int(g["week"]), "day": g["gameday"], "time": g.get("gametime", ""), "weekday": g.get("weekday", ""),
                    "away": g["away_team"], "home": g["home_team"], "stadium": g.get("stadium", ""), "venue": venue,
                    "spread": num(g.get("spread_line")) if g.get("spread_line") else None,
                    "total": num(g.get("total_line")) if g.get("total_line") else None, "weather": wx, "teams": teams})
    status["weather"] = "ok" if not wx_fail else f"{wx_fail} forecasts failed"
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "games.json"), "w") as f:
        json.dump({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "status": status, "games": out}, f, separators=(",", ":"))
    print(f"Games tab: {len(out)} games, injuries {status['injuries']}, weather {status['weather']}")


def main():
    # Red zone targets and carries from play-by-play
    rz_t, rz_c = defaultdict(int), defaultdict(int)
    drives = {}   # (game, offense, drive) -> [defense, reached red zone, result]
    for p in download(URLS["pbp"]):
        if p.get("season_type") != "REG":
            continue
        yl = num(p.get("yardline_100"))
        if p.get("posteam") and p.get("fixed_drive"):
            k = (p["game_id"], p["posteam"], p["fixed_drive"])
            d = drives.setdefault(k, [p.get("defteam", ""), False, p.get("fixed_drive_result", "")])
            if 0 < yl <= 20:
                d[1] = True
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
    try:
        build_games(drives, rows)
    except Exception as e:
        print(f"Couldn't build the Games tab: {e}")
    with open(os.path.join(OUT_DIR, "latest.json"), "w") as f:
        json.dump({"season": SEASON, "file": fname, "through_week": max_week,
                   "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}, f)

    print(f"Saved {len(rows)} player-games through week {max_week} to data/{fname}")
    print(f"Snap counts matched for {matched / n_stat_rows:.0%} of stat rows")


if __name__ == "__main__":
    main()
