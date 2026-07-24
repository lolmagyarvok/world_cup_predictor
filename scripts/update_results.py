"""
scripts/update_results.py

Napi futtatású szkript (pl. reggel 8:00).
Feladat: Lekéri az előző nap valós VB-eredményeit az API-Football-tól,
         beírja őket az adatbázisba, és frissíti a daily_predictions táblát.

Futtatás:
  python scripts/update_results.py
  python scripts/update_results.py --date 2026-06-20   # adott napra

Szükséges env változó:
  API_KEY_FOOTBALL  – api-football.com v3 API kulcs
                      (ingyenes tier: 100 hívás/nap, elegendő VB-re)
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection

# ── Konfiguráció ──────────────────────────────────────────────────────────────

API_KEY  = os.environ.get("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"
LEAGUE_ID = 1   # FIFA World Cup az api-football.com-on (ellenőrizd a sajátod!)
SEASON    = 2026

# football-data.org fallback (free tier, 10 req/perc)
FOOTBALL_DATA_KEY = os.environ.get("API_KEY_FOOTBALL-DATA-ORG", "")
FOOTBALL_DATA_URL = "https://api.football-data.org/v4/competitions/WC/matches"

ELO_K_FACTOR = 40  # VB meccsekhez ajánlott magasabb K-faktor

HEADERS = {
    "x-apisports-key": API_KEY,
}

# api-football → DB csapatnév megfeleltetés
# Bővítsd ki, ha a csapatnevekben eltérést tapasztalsz!
API_NAME_MAP = {
    "United States":             "United States",
    "South Korea":               "South Korea",
    "IR Iran":                   "Iran",
    "Korea Republic":            "South Korea",
}

# football-data.org → API-Football közös formátum (majd API_NAME_MAP megy tovább DB-be)
FD_NAME_MAP = {
    "USA":                     "United States",
    "Korea Republic":          "Korea Republic",
    "Côte d'Ivoire":           "Côte d'Ivoire",
    "IR Iran":                 "IR Iran",
    "Iran":                    "Iran",
    "Bosnia-Herzegovina":      "Bosnia-Herzegovina",
    "Curaçao":                 "Curaçao",
    "New Caledonia":           "New Caledonia",
    "DR Congo":                "DR Congo",
    "Congo DR":                "DR Congo",
    "South Korea":             "South Korea",
    "Cape Verde Islands":      "Cape Verde",
}

# ── ELO frissítés ─────────────────────────────────────────────────────────────

def _update_elo(conn, home_id: int, away_id: int, home_goals: int, away_goals: int) -> None:
    """
    Frissíti a static_elo tábla értékeit a VB-meccsen elért eredmény alapján.
    Klasszikus ELO formula, K=40.
    """
    cursor = conn.cursor()

    row_h = cursor.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (home_id,)).fetchone()
    row_a = cursor.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (away_id,)).fetchone()

    if not row_h or not row_a:
        return  # Nincs adat, kihagyjuk

    r_h = row_h["elo_rating"]
    r_a = row_a["elo_rating"]

    # Várható eredmény
    e_h = 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))
    e_a = 1.0 - e_h

    # Tényleges eredmény
    if home_goals > away_goals:
        s_h, s_a = 1.0, 0.0
    elif home_goals == away_goals:
        s_h, s_a = 0.5, 0.5
    else:
        s_h, s_a = 0.0, 1.0

    new_r_h = r_h + ELO_K_FACTOR * (s_h - e_h)
    new_r_a = r_a + ELO_K_FACTOR * (s_a - e_a)

    cursor.execute("UPDATE static_elo SET elo_rating=? WHERE team_id=?", (new_r_h, home_id))
    cursor.execute("UPDATE static_elo SET elo_rating=? WHERE team_id=?", (new_r_a, away_id))
    conn.commit()

    print(f"    ELO frissítve → {home_id}: {r_h:.0f}→{new_r_h:.0f}  |  {away_id}: {r_a:.0f}→{new_r_a:.0f}")


# ── API hívások ───────────────────────────────────────────────────────────────

def fetch_fixtures_by_date(target_date: str) -> list[dict]:
    """
    Lekéri az adott napra (YYYY-MM-DD) vonatkozó VB meccseket az API-Football-tól.
    """
    if not API_KEY:
        print("⚠️  API_KEY_FOOTBALL nincs beállítva. Töltsd ki a .env fájlban!")
        return []

    url = f"{BASE_URL}/fixtures"
    params = {
        "league": LEAGUE_ID,
        "season": SEASON,
        "date":   target_date,
        "status": "FT",  # Csak befejezett meccsek
    }

    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("errors"):
            print(f"  API hiba: {data['errors']}")
            return []

        return data.get("response", [])

    except requests.exceptions.RequestException as e:
        print(f"  Hálózati hiba az API lekérdezésnél: {e}")
        return []


def fetch_football_data_matches(target_date: str) -> list[dict]:
    """
    Fallback: football-data.org v4 (free tier).
    API-Football ingyenes csomagja nem éri el a 2026-os szezont,
    ezért ha az nem ad adatot, ezt próbáljuk.
    Visszaadja a meccseket API-Football formátumba alakítva.
    """
    if not FOOTBALL_DATA_KEY:
        print("  ℹ️  FOOTBALL_DATA_KEY nincs beállítva – football-data.org kihagyva.")
        return []

    url = f"{FOOTBALL_DATA_URL}?dateFrom={target_date}&dateTo={target_date}"
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        fixtures = []
        for match in data.get("matches", []):
            if match.get("status") not in ("FINISHED", "AWARDED"):
                continue

            ft = match.get("score", {}).get("fullTime", {})
            home_goals = ft.get("home")
            away_goals = ft.get("away")
            if home_goals is None or away_goals is None:
                continue  # Nincs teljes eredmény

            # Team name mapping (FD → API-Football közös formátum)
            raw_home = match["homeTeam"]["name"]
            raw_away = match["awayTeam"]["name"]
            home_name = FD_NAME_MAP.get(raw_home, raw_home)
            away_name = FD_NAME_MAP.get(raw_away, raw_away)

            fixtures.append({
                "fixture": {"status": {"short": "FT"}},
                "teams": {
                    "home": {"name": home_name},
                    "away": {"name": away_name},
                },
                "goals": {"home": home_goals, "away": away_goals},
            })

        if not fixtures and data.get("matches"):
            print(f"  ⚠️  football-data.org: {len(data['matches'])} meccs, de egyik sem FINISHED.")
        else:
            print(f"  ✅ football-data.org: {len(fixtures)} befejezett meccs.")

        return fixtures

    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  football-data.org hiba: {e}")
        return []
    except (KeyError, TypeError, ValueError) as e:
        print(f"  ⚠️  football-data.org adatfeldolgozási hiba: {e}")
        return []


def _normalize_name(name: str) -> str:
    """Normalizálja az API-tól kapott csapatnevet a DB névhez."""
    return API_NAME_MAP.get(name, name)


def update_match_results(conn, fixtures: list[dict]) -> int:
    """
    Megkeresi a DB-ben az adott meccseket és beírja a valós eredményt.
    Tranzakcióba csomagolva: ha bármi elszáll, a rendszer mindent visszavon (rollback).
    Visszaadja a sikeresen frissített meccsek számát.
    """
    cursor = conn.cursor()
    updated = 0

    # Csapatnév → ID szótár
    teams_in_db = cursor.execute("SELECT id, name FROM team").fetchall()
    name_to_id  = {row["name"]: row["id"] for row in teams_in_db}

    try:
        for fixture in fixtures:
            f     = fixture.get("fixture", {})
            teams = fixture.get("teams", {})
            goals = fixture.get("goals", {})

            if f.get("status", {}).get("short") != "FT":
                continue  # Csak befejezett meccs

            home_api = _normalize_name(teams.get("home", {}).get("name", ""))
            away_api = _normalize_name(teams.get("away", {}).get("name", ""))
            h_goals_raw = goals.get("home")
            a_goals_raw = goals.get("away")

            if h_goals_raw is None or a_goals_raw is None:
                print(f"  ⚠️  Hiányzó gólok: {home_api} vs {away_api}")
                continue

            # Szigorú típuskonverzió (int), mielőtt matekoznánk vagy adatbázisba írnánk
            h_goals = int(h_goals_raw)
            a_goals = int(a_goals_raw)

            home_id = name_to_id.get(home_api)
            away_id = name_to_id.get(away_api)

            if not home_id or not away_id:
                print(f"  ⚠️  Csapat nem található DB-ben: '{home_api}' / '{away_api}'")
                continue

            # Match tábla frissítése
            rows_affected = cursor.execute("""
                UPDATE match
                SET home_score = ?, away_score = ?
                WHERE home_team_id = ? AND away_team_id = ?
                  AND home_score IS NULL
                  AND EXISTS (
                      SELECT 1 FROM tournament t
                      WHERE t.id = match.tournament_id AND t.year = 2026
                  )
            """, (h_goals, a_goals, home_id, away_id)).rowcount

            if rows_affected > 0:
                print(f"  ✅ Eredmény előkészítve mentésre: {home_api} {h_goals}-{a_goals} {away_api}")
                updated += 1

                # ELO dinamikus frissítés - a Connection-t passzoljuk, mert _update_elo belül hív cursor()-t
                _update_elo(conn, home_id, away_id, h_goals, a_goals)
            else:
                print(f"  ℹ️  Kihagyva (már van eredmény vagy nincs a DB-ben): {home_api} vs {away_api}")

        # Csak és kizárólag akkor commitolunk, ha a teljes ciklus (minden meccs és ELO) hiba nélkül lefutott
        conn.commit()
        return updated

    except Exception as e:
        # Ha bármilyen kritikus hiba történik (típushiba, disconnect, adatbázis lock),
        # az egész tranzakciót eldobjuk, így nem lesznek "félig frissített" napjaink.
        conn.rollback()
        print(f"  ❌ KRITIKUS HIBA az adatbázis frissítése során: {e}. Rollback történt, semmi sem lett elmentve.")
        return 0


def mark_predictions_with_results(conn, target_date: str) -> int:
    """
    A daily_predictions táblában a tegnapi PENDING meccsekhez beírja
    az actual_winner_id-t a match tábla frissített adatai alapján.
    """
    cursor = conn.cursor()

    # Ellenőrizzük, hogy létezik-e a daily_predictions tábla
    tbl = cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='daily_predictions'
    """).fetchone()
    if not tbl:
        print("  ℹ️  'daily_predictions' tábla nem létezik, kihagyva.")
        return 0

    # PENDING predikciók keresése a dátumra
    pending = cursor.execute("""
        SELECT dp.id, dp.home_team_id, dp.away_team_id,
               m.home_score, m.away_score
        FROM daily_predictions dp
        JOIN match m ON (
            m.home_team_id = dp.home_team_id
            AND m.away_team_id = dp.away_team_id
            AND EXISTS (
                SELECT 1 FROM tournament t
                WHERE t.id = m.tournament_id AND t.year = 2026
            )
        )
        WHERE dp.match_date = ? AND dp.status = 'PENDING'
          AND m.home_score IS NOT NULL
    """, (target_date,)).fetchall()

    marked = 0
    for row in pending:
        h, a = row["home_score"], row["away_score"]
        if h > a:
            winner_id = row["home_team_id"]
        elif a > h:
            winner_id = row["away_team_id"]
        else:
            winner_id = None  # Döntetlen – actual_winner_id NULL marad

        cursor.execute("""
            UPDATE daily_predictions
            SET actual_winner_id = ?, status = 'READY_FOR_EVAL'
            WHERE id = ?
        """, (winner_id, row["id"]))
        marked += 1

    conn.commit()
    return marked


# ── Táblák inicializálása (ha még nem léteznek) ───────────────────────────────

def ensure_tables(conn) -> None:
    """
    Létrehozza a daily_predictions és daily_metrics táblákat, ha még nem léteznek.
    Biztonságosan hívható többször is.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_predictions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            match_date          TEXT NOT NULL,
            home_team_id        INTEGER NOT NULL,
            away_team_id        INTEGER NOT NULL,
            predicted_winner_id INTEGER,          -- NULL = döntetlen tipp
            odds_home           REAL DEFAULT 2.0,
            odds_draw           REAL DEFAULT 3.1,
            odds_away           REAL DEFAULT 2.0,
            target_odds         REAL DEFAULT 2.0,
            stake               REAL DEFAULT 10.0,
            status              TEXT DEFAULT 'PENDING',
            actual_winner_id    INTEGER,          -- NULL = döntetlen
            FOREIGN KEY(home_team_id)        REFERENCES team(id),
            FOREIGN KEY(away_team_id)        REFERENCES team(id),
            FOREIGN KEY(predicted_winner_id) REFERENCES team(id),
            FOREIGN KEY(actual_winner_id)    REFERENCES team(id)
        );

        CREATE TABLE IF NOT EXISTS daily_metrics (
            date                TEXT PRIMARY KEY,
            matches_evaluated   INTEGER DEFAULT 0,
            correct_predictions INTEGER DEFAULT 0,
            daily_staked        REAL DEFAULT 0.0,
            daily_returned      REAL DEFAULT 0.0,
            cumulative_roi      REAL DEFAULT 0.0,
            accuracy            REAL DEFAULT 0.0
        );
    """)
    conn.commit()


# ── Belépési pont ─────────────────────────────────────────────────────────────

def main(target_date: str | None = None) -> None:
    if target_date is None:
        target_date = str(date.today() - timedelta(days=1))  # Tegnap

    print(f"\n{'='*55}")
    print(f"  [update_results.py]  Dátum: {target_date}")
    print(f"{'='*55}")

    conn = get_connection()
    ensure_tables(conn)

    print(f"\n  1. API lekérdezés: {target_date} meccseredményei...")
    fixtures = fetch_fixtures_by_date(target_date)
    print(f"     {len(fixtures)} befejezett meccs (API-Football).")

    # Ha API-Football nem adott adatot (free plan limit), próbáljuk a football-data.org-ot
    if not fixtures:
        print("\n  ⏩ Fallback: football-data.org...")
        fixtures = fetch_football_data_matches(target_date)
        print(f"     {len(fixtures)} befejezett meccs (football-data.org).")

    if fixtures:
        print("\n  2. Eredmények mentése az adatbázisba...")
        updated = update_match_results(conn, fixtures)
        print(f"     {updated} meccs eredménye frissítve.")

        print("\n  3. Predikciók státuszának frissítése (PENDING → READY_FOR_EVAL)...")
        marked = mark_predictions_with_results(conn, target_date)
        print(f"     {marked} predikció kész a kiértékelésre.")
    else:
        print("  ⚠️  Nem érkezett meccsadat – egyik API sem adott eredményt.")
        print("       Ellenőrizd az API kulcsokat a .env fájlban.")

    conn.close()
    print(f"\n  ✅ update_results.py kész.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VB napi eredményfrissítő")
    parser.add_argument("--date", type=str, default=None,
                        help="Dátum YYYY-MM-DD formátumban (alapért.: tegnap)")
    args = parser.parse_args()
    main(args.date)