import csv
import sqlite3
from pathlib import Path
from typing import Optional

HOST_COUNTRIES: dict[int, tuple[str, str]] = {
    2002: ("South Korea / Japan", "AFC"),
    2006: ("Germany",             "UEFA"),
    2010: ("South Africa",        "CAF"),
    2014: ("Brazil",              "CONMEBOL"),
    2018: ("Russia",              "UEFA"),
    2022: ("Qatar",               "AFC"),
    2026: ("USA / Canada / Mexico", "CONCACAF"),
}

# FIFA kód lookup – ahol a CSV nem adja meg
FIFA_CODES: dict[str, str] = {
    "Argentina":     "ARG", "Australia":     "AUS", "Belgium":    "BEL",
    "Brazil":        "BRA", "Cameroon":      "CMR", "Costa Rica": "CRC",
    "Croatia":       "CRO", "Denmark":       "DEN", "Ecuador":    "ECU",
    "England":       "ENG", "France":        "FRA", "Germany":    "GER",
    "Ghana":         "GHA", "Iran":          "IRN", "Japan":      "JPN",
    "Mexico":        "MEX", "Morocco":       "MAR", "Netherlands":"NED",
    "Poland":        "POL", "Portugal":      "POR", "Qatar":      "QAT",
    "Saudi Arabia":  "KSA", "Senegal":       "SEN", "Serbia":     "SRB",
    "South Korea":   "KOR", "Spain":         "ESP", "Switzerland":"SUI",
    "Tunisia":       "TUN", "United States": "USA", "Uruguay":    "URU",
    "Wales":         "WAL", "Canada":        "CAN",
    "South Africa":  "RSA", "Ivory Coast":   "CIV", "Algeria":    "ALG",
    "Nigeria":       "NGA", "Togo":          "TOG", "Angola":     "ANG",
    "Paraguay":      "PAR", "Honduras":      "HON", "New Zealand":"NZL",
    "Slovakia":      "SVK", "Slovenia":      "SVN", "Greece":     "GRE",
    "Chile":         "CHI", "Colombia":      "COL", "Costa Rica": "CRC",
    "Bosnia":        "BIH", "Russia":        "RUS", "Ukraine":    "UKR",
    "Sweden":        "SWE", "Austria":       "AUT", "Czech Republic": "CZE",
    "Turkey":        "TUR", "Italy":         "ITA", "North Korea":"PRK",
}


def _safe_int(val: str) -> Optional[int]:
    try:
        return int(float(val.strip())) if val.strip() else None
    except (ValueError, AttributeError):
        return None


def _safe_float(val: str) -> Optional[float]:
    try:
        return float(val.strip()) if val.strip() else None
    except (ValueError, AttributeError):
        return None


def _get_or_create_tournament(conn: sqlite3.Connection, year: int) -> int:
    host, host_cont = HOST_COUNTRIES.get(year, ("Unknown", "Unknown"))
    row = conn.execute("SELECT id FROM tournament WHERE year = ?", (year,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO tournament(year, host_country, host_continent) VALUES (?,?,?)",
        (year, host, host_cont),
    )
    return cur.lastrowid


def _get_or_create_team(conn: sqlite3.Connection, name: str, continent: str) -> int:
    name = name.strip()
    row = conn.execute("SELECT id FROM team WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    fifa_code = FIFA_CODES.get(name, name[:3].upper())
    row_by_code = conn.execute("SELECT id FROM team WHERE fifa_code = ?", (fifa_code,)).fetchone()
    if row_by_code:
        return row_by_code["id"]
    cur = conn.execute(
        "INSERT INTO team(name, fifa_code, continent) VALUES (?,?,?)",
        (name, fifa_code, continent.strip()),
    )
    return cur.lastrowid


def load_csv(csv_path: Path, conn: sqlite3.Connection) -> int:
    """
    Betölti a CSV-t. Visszaadja a beillesztett/frissített sorok számát.
    Idempotens: UPSERT-et használ, futtatható többször is.
    """
    inserted = 0

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)

        with conn:
            for row in reader:
                year = _safe_int(row.get("version", ""))
                if year is None:
                    print(f"[csv] Kihagyva – hiányzó year: {row}")
                    continue

                tournament_id = _get_or_create_tournament(conn, year)
                team_id = _get_or_create_team(
                    conn,
                    row.get("team", ""),
                    row.get("continent", ""),
                )

                conn.execute(
                    """
                    INSERT INTO team_tournament_stat (
                        team_id, tournament_id,
                        is_host,
                        fifa_rank_pre, fifa_points_pre,
                        squad_total_value_eur, squad_avg_age,
                        goals_scored_last_4y, goals_received_last_4y,
                        wins_last_4y, draws_last_4y, losses_last_4y,
                        world_cup_titles_before, world_cup_participations_before,
                        groups_passed_before, round16_before,
                        quarterfinals_before, semifinals_before, finals_before,
                        winner, finalist, semi_finalist, quarter_finalist
                    ) VALUES (
                        ?,?,  ?,  ?,?,  ?,?,  ?,?,  ?,?,?,  ?,?,  ?,?,?,?,?,  ?,?,?,?
                    )
                    ON CONFLICT(team_id, tournament_id) DO UPDATE SET
                        is_host                     = excluded.is_host,
                        fifa_rank_pre               = excluded.fifa_rank_pre,
                        fifa_points_pre             = excluded.fifa_points_pre,
                        squad_total_value_eur       = excluded.squad_total_value_eur,
                        squad_avg_age               = excluded.squad_avg_age,
                        goals_scored_last_4y        = excluded.goals_scored_last_4y,
                        goals_received_last_4y      = excluded.goals_received_last_4y,
                        wins_last_4y                = excluded.wins_last_4y,
                        draws_last_4y               = excluded.draws_last_4y,
                        losses_last_4y              = excluded.losses_last_4y,
                        world_cup_titles_before     = excluded.world_cup_titles_before,
                        world_cup_participations_before = excluded.world_cup_participations_before,
                        groups_passed_before        = excluded.groups_passed_before,
                        round16_before              = excluded.round16_before,
                        quarterfinals_before        = excluded.quarterfinals_before,
                        semifinals_before           = excluded.semifinals_before,
                        finals_before               = excluded.finals_before,
                        winner                      = excluded.winner,
                        finalist                    = excluded.finalist,
                        semi_finalist               = excluded.semi_finalist,
                        quarter_finalist            = excluded.quarter_finalist
                    """,
                    (
                        team_id, tournament_id,
                        _safe_int(row.get("is_host", "0")) or 0,
                        _safe_int(row.get("fifa_rank_pre_tournament", "")),
                        _safe_float(row.get("fifa_points_pre_tournament", "")),
                        _safe_float(row.get("squad_total_market_value_eur", "")),
                        _safe_float(row.get("squad_avg_age", "")),
                        _safe_int(row.get("goals_scored_last_4y", "0")) or 0,
                        _safe_int(row.get("goals_received_last_4y", "0")) or 0,
                        _safe_int(row.get("wins_last_4y", "0")) or 0,
                        _safe_int(row.get("draws_last_4y", "0")) or 0,
                        _safe_int(row.get("losses_last_4y", "0")) or 0,
                        _safe_int(row.get("world_cup_titles_before", "0")) or 0,
                        _safe_int(row.get("world_cup_participations_before", "0")) or 0,
                        _safe_int(row.get("groups_passed_before", "0")) or 0,
                        _safe_int(row.get("round16_before", "0")) or 0,
                        _safe_int(row.get("quarterfinals_before", "0")) or 0,
                        _safe_int(row.get("semifinals_before", "0")) or 0,
                        _safe_int(row.get("finals_before", "0")) or 0,
                        _safe_int(row.get("winner", "0")) or 0,
                        _safe_int(row.get("finalist", "0")) or 0,
                        _safe_int(row.get("semi_finalist", "0")) or 0,
                        _safe_int(row.get("quarter_finalist", "0")) or 0,
                    ),
                )
                inserted += 1

    print(f"[csv] {inserted} sor betöltve/frissítve – {csv_path.name}")
    return inserted


if __name__ == "__main__":
    import sys
    from db import get_connection, init_db

    if len(sys.argv) < 2:
        print("Használat: python csv_loader.py <path/to/file.csv>")
        sys.exit(1)

    init_db()
    conn = get_connection()
    load_csv(Path(sys.argv[1]), conn)
    conn.close()