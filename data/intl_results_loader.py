"""
parsers/intl_results_loader.py

Betölti a martj42/international_results CSV-t.
Ez a legfontosabb forrás az ELO számításhoz – 1872-től napjainkig
tartalmaz MINDEN válogatott meccset.

Forrás: https://raw.githubusercontent.com/martj42/international_results/master/results.csv

CSV fejléc:
  date, home_team, away_team, home_score, away_score,
  tournament, city, country, neutral

Stratégia:
  - Csak VB és VB-selejtező meccseket töltünk be a match táblába
    (a többi match csak az ELO számításhoz kell, azt a features/ modul kezeli)
  - Az ELO log-ot itt NEM töltjük – azt a features/elo.py számítja
    az összes meccs alapján (beleértve a barátságosokat is, súlyozva)
  - A "neutral" flag eltárolódik – ez fontos feature (pályaválasztás hatása)

Futtatás:
  python intl_results_loader.py            # WC + qualifier meccsek
  python intl_results_loader.py --all      # minden meccs (lassabb, ~50k sor)
"""

import csv
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Optional

SOURCE_URL = (
    "https://raw.githubusercontent.com/martj42/international_results"
    "/master/results.csv"
)

# Csak ezeket töltjük be a match táblába
WC_TOURNAMENTS = {
    "FIFA World Cup",
    "FIFA World Cup qualification",
    "FIFA World Cup qualification (CONMEBOL)",
    "FIFA World Cup qualification (UEFA)",
    "FIFA World Cup qualification (CAF)",
    "FIFA World Cup qualification (AFC)",
    "FIFA World Cup qualification (CONCACAF)",
    "FIFA World Cup qualification (OFC)",
    "FIFA World Cup qualification (inter-confederation play-offs)",
}

# Kontinens lookup (nem teljes, de fedi a VB résztvevőket)
CONTINENT_BY_COUNTRY: dict[str, str] = {
    "Brazil": "CONMEBOL", "Argentina": "CONMEBOL", "Uruguay": "CONMEBOL",
    "Colombia": "CONMEBOL", "Chile": "CONMEBOL", "Ecuador": "CONMEBOL",
    "Paraguay": "CONMEBOL", "Peru": "CONMEBOL", "Bolivia": "CONMEBOL",
    "Venezuela": "CONMEBOL",
    "France": "UEFA", "Germany": "UEFA", "Spain": "UEFA", "England": "UEFA",
    "Italy": "UEFA", "Netherlands": "UEFA", "Portugal": "UEFA", "Belgium": "UEFA",
    "Croatia": "UEFA", "Denmark": "UEFA", "Sweden": "UEFA", "Switzerland": "UEFA",
    "Poland": "UEFA", "Serbia": "UEFA", "Austria": "UEFA", "Ukraine": "UEFA",
    "Wales": "UEFA", "Scotland": "UEFA", "Hungary": "UEFA", "Czech Republic": "UEFA",
    "Slovakia": "UEFA", "Slovenia": "UEFA", "Greece": "UEFA", "Turkey": "UEFA",
    "Russia": "UEFA", "Romania": "UEFA",
    "Brazil": "CONMEBOL",
    "United States": "CONCACAF", "Mexico": "CONCACAF", "Canada": "CONCACAF",
    "Costa Rica": "CONCACAF", "Honduras": "CONCACAF", "Panama": "CONCACAF",
    "Jamaica": "CONCACAF", "Trinidad and Tobago": "CONCACAF",
    "Japan": "AFC", "South Korea": "AFC", "Iran": "AFC", "Saudi Arabia": "AFC",
    "Australia": "AFC", "Qatar": "AFC", "Iraq": "AFC", "UAE": "AFC",
    "Cameroon": "CAF", "Nigeria": "CAF", "Ghana": "CAF", "Senegal": "CAF",
    "Morocco": "CAF", "Tunisia": "CAF", "Algeria": "CAF", "Egypt": "CAF",
    "Ivory Coast": "CAF", "South Africa": "CAF", "Angola": "CAF", "Togo": "CAF",
    "New Zealand": "OFC",
}


def _get_continent(country: str) -> str:
    return CONTINENT_BY_COUNTRY.get(country, "Unknown")


def _make_unique_code(conn: sqlite3.Connection, name: str) -> str:
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
    continent = _get_continent(name)
    code = _make_unique_code(conn, name)
    cur = conn.execute(
        "INSERT INTO team(name,fifa_code,continent) VALUES(?,?,?)",
        (name, code, continent),
    )
    return cur.lastrowid


def _get_tournament_year(date_str: str) -> int:
    return int(date_str[:4])


def _get_or_create_tournament(
    conn: sqlite3.Connection,
    year: int,
    tournament_name: str,
) -> int:
    # VB selejtezőket ugyanabba a tornába rakjuk mint a VB-t
    # (a 'stage' mező különbözteti meg őket)
    effective_name = "FIFA World Cup" if "qualification" in tournament_name.lower() else tournament_name
    row = conn.execute("SELECT id FROM tournament WHERE year=?", (year,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO tournament(year,host_country,host_continent) VALUES(?,'Unknown','Unknown')",
        (year,),
    )
    return cur.lastrowid


def _safe_int(val: str) -> Optional[int]:
    try:
        return int(val.strip())
    except (ValueError, AttributeError):
        return None


def load_intl_results(
    conn: sqlite3.Connection,
    source: str = SOURCE_URL,
    wc_only: bool = True,
    min_year: int = 2002,
) -> dict:
    """
    Betölti a CSV-t.

    wc_only=True  → csak WC és WC qualifier meccsek kerülnek a match táblába
    wc_only=False → minden meccs (barátságosak is) – csak ELO calibrációhoz
    min_year      → ennél régebbi meccseket kihagyja
    """
    stats = {"loaded": 0, "skipped": 0, "errors": 0}

    req = urllib.request.Request(source, headers={"User-Agent": "vb-predictor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        lines = r.read().decode("utf-8").splitlines()

    reader = csv.DictReader(lines)

    batch: list[tuple] = []
    BATCH_SIZE = 500

    def flush(batch: list[tuple]) -> None:
        conn.executemany(
            """INSERT INTO match(
                tournament_id, home_team_id, away_team_id,
                stage, match_date, city,
                home_score, away_score
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING""",
            batch,
        )

    with conn:
        for row in reader:
            date       = row["date"].strip()
            year       = _get_tournament_year(date)
            tournament = row["tournament"].strip()
            is_neutral = row.get("neutral", "").strip().upper() == "TRUE"

            if year < min_year:
                stats["skipped"] += 1
                continue

            is_wc_related = tournament in WC_TOURNAMENTS
            if wc_only and not is_wc_related:
                stats["skipped"] += 1
                continue

            home_name = row["home_team"].strip()
            away_name = row["away_team"].strip()
            home_score = _safe_int(row.get("home_score", ""))
            away_score = _safe_int(row.get("away_score", ""))

            if not home_name or not away_name:
                stats["errors"] += 1
                continue

            home_id = _get_or_create_team(conn, home_name)
            away_id = _get_or_create_team(conn, away_name)

            tournament_id = _get_or_create_tournament(conn, year, tournament)

            stage = "Qualifier" if "qualification" in tournament.lower() else "Group stage"

            city = row.get("city", "").strip() or None

            batch.append((
                tournament_id, home_id, away_id,
                stage, date, city,
                home_score, away_score,
            ))

            stats["loaded"] += 1

            if len(batch) >= BATCH_SIZE:
                flush(batch)
                batch.clear()

        if batch:
            flush(batch)

    return stats


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db import get_connection, init_db

    wc_only = "--all" not in sys.argv
    min_year = 2002

    for arg in sys.argv[1:]:
        if arg.startswith("--from="):
            min_year = int(arg.split("=")[1])

    init_db()
    conn = get_connection()

    print(f"[intl] Letöltés... (wc_only={wc_only}, min_year={min_year})")
    stats = load_intl_results(conn, wc_only=wc_only, min_year=min_year)
    conn.close()

    print(
        f"[intl] Kész – {stats['loaded']} betöltve, "
        f"{stats['skipped']} kihagyva, {stats['errors']} hiba"
    )
