"""
parsers/worldcup_json_loader.py

Betölti az openfootball/worldcup.json adatait a DB-be.
Forrás: https://github.com/openfootball/worldcup.json

JSON struktúra:
{
  "name": "World Cup 2022",
  "matches": [
    {
      "round": "Matchday 1",
      "date": "2022-11-20",
      "time": "19:00",
      "team1": "Qatar",
      "team2": "Ecuador",
      "score": {"ft": [0, 2], "ht": [0, 2]},
      "goals1": [],
      "goals2": [{"name": "Enner Valencia", "minute": 16, "penalty": true}],
      "group": "Group A",
      "ground": "Al Bayt Stadium, Al Khor"
    }, ...
  ]
}

Futtatás:
  python worldcup_json_loader.py          # letölti az összes VB-t
  python worldcup_json_loader.py 2022     # csak 2022
"""

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Optional

# VB évek és a hozzájuk tartozó raw URL-ek
WC_SOURCES: dict[int, str] = {
    2002: "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2002/worldcup.json",
    2006: "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2006/worldcup.json",
    2010: "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2010/worldcup.json",
    2014: "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2014/worldcup.json",
    2018: "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2018/worldcup.json",
    2022: "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2022/worldcup.json",
    2026: "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json",
}

HOST_INFO: dict[int, tuple[str, str]] = {
    2002: ("South Korea / Japan", "AFC"),
    2006: ("Germany",             "UEFA"),
    2010: ("South Africa",        "CAF"),
    2014: ("Brazil",              "CONMEBOL"),
    2018: ("Russia",              "UEFA"),
    2022: ("Qatar",               "AFC"),
    2026: ("USA / Canada / Mexico", "CONCACAF"),
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _fetch_json(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vb-predictor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  [warn] Nem sikerült letölteni: {url} → {e}")
        return None


def _get_or_create_tournament(conn: sqlite3.Connection, year: int) -> int:
    row = conn.execute("SELECT id FROM tournament WHERE year=?", (year,)).fetchone()
    if row:
        return row["id"]
    host, host_cont = HOST_INFO.get(year, ("Unknown", "Unknown"))
    cur = conn.execute(
        "INSERT INTO tournament(year,host_country,host_continent) VALUES(?,?,?)",
        (year, host, host_cont),
    )
    return cur.lastrowid


def _make_unique_code(conn: sqlite3.Connection, name: str) -> str:
    """Ütközésmentes FIFA kód generálás."""
    base = name[:3].upper().replace(" ", "")
    code = base
    i = 1
    while conn.execute("SELECT 1 FROM team WHERE fifa_code=?", (code,)).fetchone():
        code = f"{base}{i}"
        i += 1
    return code


def _get_or_create_team(conn: sqlite3.Connection, name: str) -> int:
    name = name.strip()
    row = conn.execute("SELECT id FROM team WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    code = _make_unique_code(conn, name)
    cur = conn.execute(
        "INSERT INTO team(name,fifa_code,continent) VALUES(?,?,'Unknown')",
        (name, code),
    )
    return cur.lastrowid


def _get_or_create_player(conn: sqlite3.Connection, name: str, nationality: str) -> int:
    name = name.strip()
    row = conn.execute(
        "SELECT id FROM player WHERE name=? AND nationality=?", (name, nationality)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO player(name,nationality,position) VALUES(?,?,'FWD')",
        (name, nationality),
    )
    return cur.lastrowid


def _parse_stage(round_str: str, group: Optional[str]) -> str:
    """
    "Matchday 1" + "Group A" → "Group A"
    "Round of 16" → "Round of 16"
    "Final" → "Final"
    """
    r = round_str.strip()
    if group:
        return group.strip()
    return r


def _parse_venue(ground: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'Al Bayt Stadium, Al Khor' → ('Al Bayt Stadium', 'Al Khor')"""
    if not ground:
        return None, None
    parts = ground.split(",", 1)
    venue = parts[0].strip()
    city = parts[1].strip() if len(parts) > 1 else None
    return venue, city


# ── core loader ───────────────────────────────────────────────────────────────

def load_worldcup(year: int, conn: sqlite3.Connection, data: dict) -> dict:
    stats = {"matches": 0, "goals": 0, "skipped": 0}

    tournament_id = _get_or_create_tournament(conn, year)
    team_fifa_code: dict[str, str] = {}   # name → fifa_code cache

    for raw_match in data.get("matches", []):
        round_name = raw_match.get("round", "")
        group      = raw_match.get("group")
        stage      = _parse_stage(round_name, group)
        date       = raw_match.get("date")
        time_      = raw_match.get("time")
        venue, city = _parse_venue(raw_match.get("ground"))

        t1_name = raw_match.get("team1", "").strip()
        t2_name = raw_match.get("team2", "").strip()

        # Placeholder csapatok (pl. "W101") kihagyva – ezek még nem dőltek el
        if t1_name.startswith("W") and t1_name[1:].isdigit():
            stats["skipped"] += 1
            continue
        if t2_name.startswith("W") and t2_name[1:].isdigit():
            stats["skipped"] += 1
            continue

        if not t1_name or not t2_name:
            stats["skipped"] += 1
            continue

        home_id = _get_or_create_team(conn, t1_name)
        away_id = _get_or_create_team(conn, t2_name)

        score   = raw_match.get("score", {})
        ft      = score.get("ft")   # [home, away] rendes játékidő
        aet     = score.get("aet")  # [home, away] hosszabbítás után
        pens    = score.get("p")    # [home, away] büntetők

        home_score = ft[0]  if ft   else None
        away_score = ft[1]  if ft   else None
        home_aet   = aet[0] if aet  else None
        away_aet   = aet[1] if aet  else None
        home_pens  = pens[0] if pens else None
        away_pens  = pens[1] if pens else None

        # Idempotens UPSERT meccsekre (dátum + két csapat egyedi kulcs)
        existing = conn.execute(
            "SELECT id FROM match WHERE tournament_id=? AND home_team_id=? AND away_team_id=? AND match_date=?",
            (tournament_id, home_id, away_id, date),
        ).fetchone()

        if existing:
            match_id = existing["id"]
            conn.execute(
                """UPDATE match SET
                    stage=?, match_time=?, venue=?, city=?,
                    home_score=?, away_score=?,
                    home_score_aet=?, away_score_aet=?,
                    home_pens=?, away_pens=?
                WHERE id=?""",
                (stage, time_, venue, city,
                 home_score, away_score,
                 home_aet, away_aet,
                 home_pens, away_pens,
                 match_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO match(
                    tournament_id, home_team_id, away_team_id,
                    stage, match_date, match_time, venue, city,
                    home_score, away_score,
                    home_score_aet, away_score_aet,
                    home_pens, away_pens
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tournament_id, home_id, away_id,
                 stage, date, time_, venue, city,
                 home_score, away_score,
                 home_aet, away_aet,
                 home_pens, away_pens),
            )
            match_id = cur.lastrowid
            stats["matches"] += 1

        # ── Gólok ──────────────────────────────────────────────────────
        # Töröljük a régi gól bejegyzéseket, majd újra felvesszük
        conn.execute("DELETE FROM goal_event WHERE match_id=?", (match_id,))

        for side, team_id, team_name in [
            ("goals1", home_id, t1_name),
            ("goals2", away_id, t2_name),
        ]:
            for goal in raw_match.get(side, []):
                scorer_name = goal.get("name", "").strip()
                minute      = goal.get("minute", 0)
                is_penalty  = int(bool(goal.get("penalty", False)))
                is_og       = int(bool(goal.get("owngoal", False)))

                # Own goal esetén a csapat ellenfél, de a gólszerzőt az eredeti oldalon keressük
                scoring_team_id = away_id if (is_og and side == "goals1") else \
                                  home_id if (is_og and side == "goals2") else team_id

                player_id = None
                if scorer_name:
                    player_id = _get_or_create_player(conn, scorer_name, team_name[:3].upper())

                conn.execute(
                    """INSERT INTO goal_event(match_id,player_id,team_id,minute,is_penalty,is_own_goal)
                       VALUES(?,?,?,?,?,?)""",
                    (match_id, player_id, scoring_team_id, minute, is_penalty, is_og),
                )
                stats["goals"] += 1

    return stats


# ── entry point ───────────────────────────────────────────────────────────────

def run(years: Optional[list[int]], conn: sqlite3.Connection) -> None:
    targets = years if years else sorted(WC_SOURCES.keys())

    for year in targets:
        url = WC_SOURCES.get(year)
        if not url:
            print(f"[wc] Nincs URL konfigurálva: {year}")
            continue

        print(f"[wc] {year} letöltése...")
        data = _fetch_json(url)
        if data is None:
            print(f"[wc] {year} kihagyva (letöltési hiba)")
            continue

        with conn:
            stats = load_worldcup(year, conn, data)

        print(
            f"[wc] {year} kész – "
            f"{stats['matches']} meccs, {stats['goals']} gól, "
            f"{stats['skipped']} kihagyva (placeholder)"
        )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db import get_connection, init_db

    init_db()
    conn = get_connection()

    years_arg = [int(y) for y in sys.argv[1:]] if len(sys.argv) > 1 else None
    run(years_arg, conn)
    conn.close()
