"""
parsers/club_form_loader.py

Betölti az openfootball/football.json liga adatokat.
Cél: NEM a meccs táblát töltjük (az csak válogatott meccseket tartalmaz),
hanem egy külön 'club_match' és 'club_team' táblát – ezekből számolható
a játékosok klub-forma értéke (pl. utolsó 10 meccsen mennyit játszottak).

Támogatott ligák és URL-ek:
  en.1  → Premier League
  de.1  → Bundesliga
  es.1  → La Liga
  it.1  → Serie A
  fr.1  → Ligue 1
  pt.1  → Primeira Liga

Futtatás:
  python club_form_loader.py 2024-25       # aktuális szezon
  python club_form_loader.py 2023-24 2024-25  # több szezon
"""

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Optional

BASE_URL = "https://raw.githubusercontent.com/openfootball/football.json/master"

# Liga kód → (fájlnév, ország, kontinens)
LEAGUES: dict[str, tuple[str, str, str]] = {
    "en.1": ("en.1.json", "England",  "UEFA"),
    "de.1": ("de.1.json", "Germany",  "UEFA"),
    "es.1": ("es.1.json", "Spain",    "UEFA"),
    "it.1": ("it.1.json", "Italy",    "UEFA"),
    "fr.1": ("fr.1.json", "France",   "UEFA"),
    "pt.1": ("pt.1.json", "Portugal", "UEFA"),
}

# VB-n részt vevő csapatok klubjai – ez a szűrő
# Ha csak a VB-s játékosok klubformáját akarjuk, szűkíthetünk
# None = minden klub


def _fetch_json(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vb-predictor/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  [warn] Letöltési hiba: {url} → {e}")
        return None


def _ensure_club_tables(conn: sqlite3.Connection) -> None:
    """Club-specifikus táblák, amelyek a fő sémától elkülönülnek."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS club_team (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT    NOT NULL UNIQUE,
            country TEXT    NOT NULL,
            league  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS club_match (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            home_club_id    INTEGER NOT NULL REFERENCES club_team(id),
            away_club_id    INTEGER NOT NULL REFERENCES club_team(id),
            match_date      TEXT    NOT NULL,
            season          TEXT    NOT NULL,    -- "2024-25"
            league          TEXT    NOT NULL,    -- "en.1"
            round           TEXT,
            home_score      INTEGER,
            away_score      INTEGER,
            UNIQUE(home_club_id, away_club_id, match_date)
        );

        CREATE INDEX IF NOT EXISTS idx_club_match_date   ON club_match(match_date);
        CREATE INDEX IF NOT EXISTS idx_club_match_home   ON club_match(home_club_id);
        CREATE INDEX IF NOT EXISTS idx_club_match_away   ON club_match(away_club_id);
    """)


def _get_or_create_club(
    conn: sqlite3.Connection,
    name: str,
    country: str,
    league: str,
) -> int:
    name = name.strip()
    row = conn.execute("SELECT id FROM club_team WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO club_team(name,country,league) VALUES(?,?,?)",
        (name, country, league),
    )
    return cur.lastrowid


def load_league(
    season: str,
    league_code: str,
    conn: sqlite3.Connection,
) -> dict:
    """Egy liga egy szezonjának betöltése."""
    filename, country, _ = LEAGUES[league_code]
    url = f"{BASE_URL}/{season}/{filename}"

    data = _fetch_json(url)
    if data is None:
        return {"loaded": 0, "error": True}

    stats = {"loaded": 0, "skipped": 0}

    with conn:
        for match in data.get("matches", []):
            t1 = match.get("team1", "").strip()
            t2 = match.get("team2", "").strip()
            date = match.get("date")
            round_ = match.get("round")

            if not t1 or not t2 or not date:
                stats["skipped"] += 1
                continue

            score = match.get("score", {})
            ft = score.get("ft")
            home_score = ft[0] if ft else None
            away_score = ft[1] if ft else None

            home_id = _get_or_create_club(conn, t1, country, league_code)
            away_id = _get_or_create_club(conn, t2, country, league_code)

            conn.execute(
                """INSERT INTO club_match(
                    home_club_id, away_club_id,
                    match_date, season, league, round,
                    home_score, away_score
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(home_club_id,away_club_id,match_date) DO UPDATE SET
                    home_score=excluded.home_score,
                    away_score=excluded.away_score""",
                (home_id, away_id, date, season, league_code, round_,
                 home_score, away_score),
            )
            stats["loaded"] += 1

    return stats


def run(seasons: list[str], conn: sqlite3.Connection) -> None:
    _ensure_club_tables(conn)

    for season in seasons:
        for league_code in LEAGUES:
            print(f"[club] {season} / {league_code}...", end=" ")
            stats = load_league(season, league_code, conn)
            if stats.get("error"):
                print("HIBA (valószínűleg nincs meg ez a szezon)")
            else:
                print(f"{stats['loaded']} meccs")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db import get_connection, init_db

    seasons = sys.argv[1:] if len(sys.argv) > 1 else ["2024-25"]

    init_db()
    conn = get_connection()
    run(seasons, conn)
    conn.close()
