"""
parsers/statsbomb_loader.py

StatsBomb open data betöltése: lineup, xG, lapok.
Ingyenesen elérhető: 2018, 2022 (+ régebbi VB-k).

Feltölti:
  - match_lineup  (starter/csere, pozíció, be/kijátszott percek)
  - goal_event    (xG mező frissítése ahol hiányzott)
  - card_event    (sárga/piros lapok)
  - player        (pozíció, nemzetiség pontosítása)

Futtatás:
  python statsbomb_loader.py          # 2018 + 2022
  python statsbomb_loader.py 2022     # csak 2022
"""

import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

try:
    from statsbombpy import sb
except ImportError as e:
    print(f"VALÓDI HIBA {e}")
    print("[sb] pip install statsbombpy --break-system-packages")
    sys.exit(1)

# StatsBomb competition_id=43 → FIFA World Cup
# season_id → év
SEASONS: dict[int, int] = {
    2018: 3,
    2022: 106,
}

# StatsBomb pozíció → DB enum
POSITION_MAP: dict[str, str] = {
    "Goalkeeper":            "GK",
    "Right Back":            "DEF", "Left Back":         "DEF",
    "Right Center Back":     "DEF", "Left Center Back":  "DEF",
    "Center Back":           "DEF",
    "Right Wing Back":       "DEF", "Left Wing Back":    "DEF",
    "Right Midfield":        "MID", "Left Midfield":     "MID",
    "Right Center Midfield": "MID", "Left Center Midfield": "MID",
    "Center Midfield":       "MID", "Attacking Midfield": "MID",
    "Defensive Midfield":    "MID",
    "Right Wing":            "FWD", "Left Wing":         "FWD",
    "Right Center Forward":  "FWD", "Left Center Forward": "FWD",
    "Center Forward":        "FWD", "Secondary Striker": "FWD",
}

CARD_MAP: dict[str, str] = {
    "Yellow Card":    "yellow",
    "Red Card":       "red",
    "Second Yellow":  "yellow_red",
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _find_match(conn: sqlite3.Connection, year: int,
                home: str, away: str, date: str):
    row = conn.execute("""
        SELECT m.id FROM match m
        JOIN team h ON h.id = m.home_team_id
        JOIN team a ON a.id = m.away_team_id
        JOIN tournament t ON t.id = m.tournament_id
        WHERE t.year = ? AND h.name = ? AND a.name = ?
          AND m.match_date = ?
    """, (year, home, away, date)).fetchone()
    return row["id"] if row else None


def _get_or_create_player(conn: sqlite3.Connection,
                           name: str, nationality: str,
                           position: str = "FWD") -> int:
    row = conn.execute(
        "SELECT id FROM player WHERE name=? AND nationality=?",
        (name, nationality)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO player(name, nationality, position) VALUES(?,?,?)",
        (name, nationality, position)
    )
    return cur.lastrowid


def _resolve_team_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM team WHERE name=?", (name,)).fetchone()
    return row["id"] if row else None


# ── lineup ────────────────────────────────────────────────────────────────────

def _load_lineup(conn: sqlite3.Connection, match_id: int,
                 team_id: int, team_name: str, lineup_df) -> int:
    """Egy csapat lineupját tölti be a match_lineup táblába."""
    conn.execute("DELETE FROM match_lineup WHERE match_id=? AND team_id=?",
                 (match_id, team_id))

    loaded = 0
    for _, row in lineup_df.iterrows():
        player_name = row["player_name"]
        nationality = row.get("country", team_name)
        positions   = row.get("positions", []) or []

        # Elsődleges pozíció meghatározása
        sb_pos = positions[0]["position"] if positions else "Center Forward"
        db_pos = POSITION_MAP.get(sb_pos, "MID")

        player_id = _get_or_create_player(
            conn, player_name, nationality, db_pos
        )
        # Pozíció frissítése ha már ismert játékos volt "FWD"-ként
        conn.execute(
            "UPDATE player SET position=? WHERE id=? AND position='FWD'",
            (db_pos, player_id)
        )

        # Starter / csere és percek
        is_starter = 1
        minute_in  = None
        minute_out = None

        for pos_entry in positions:
            start_reason = pos_entry.get("start_reason", "")
            end_reason   = pos_entry.get("end_reason", "")

            if "Substitution" in start_reason:
                is_starter = 0
                # "from": "64:10" → 64
                from_str = pos_entry.get("from", "0:0")
                try:
                    minute_in = int(from_str.split(":")[0])
                except ValueError:
                    minute_in = None

            if "Substitution" in end_reason:
                to_str = pos_entry.get("to", "")
                try:
                    minute_out = int(to_str.split(":")[0])
                except ValueError:
                    minute_out = None

        conn.execute("""
            INSERT OR IGNORE INTO match_lineup
              (match_id, player_id, team_id, is_starter, minute_in, minute_out)
            VALUES (?,?,?,?,?,?)
        """, (match_id, player_id, team_id, is_starter, minute_in, minute_out))
        loaded += 1

    return loaded


# ── xG + gólok ────────────────────────────────────────────────────────────────

def _update_xg(conn: sqlite3.Connection, match_id: int, shots_df) -> None:
    """
    A goal_event táblában lévő gólok xG értékét frissíti.
    A séma egyelőre nem tartalmaz xG oszlopot a goal_event-ben –
    hozzáadjuk ha hiányzik.
    """
    # Oszlop hozzáadása ha nincs (idempotens)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(goal_event)").fetchall()]
    if "xg" not in cols:
        conn.execute("ALTER TABLE goal_event ADD COLUMN xg REAL")

    if shots_df is None or shots_df.empty:
        return

    goals = shots_df[shots_df["shot_outcome"] == "Goal"]
    for _, shot in goals.iterrows():
        xg      = shot.get("shot_statsbomb_xg")
        minute  = shot.get("minute")
        p_name  = shot.get("player", "")

        if xg is None or minute is None:
            continue

        conn.execute("""
            UPDATE goal_event SET xg=?
            WHERE match_id=? AND minute=?
              AND player_id IN (
                  SELECT id FROM player WHERE name=?
              )
        """, (float(xg), match_id, int(minute), p_name))


# ── lapok ─────────────────────────────────────────────────────────────────────

def _load_cards(conn: sqlite3.Connection, match_id: int,
                team_id: int, team_name: str, lineup_df) -> int:
    """Lapokat tölt be a card_event táblába a lineup 'cards' mezőjéből."""
    conn.execute("DELETE FROM card_event WHERE match_id=? AND team_id=?",
                 (match_id, team_id))
    loaded = 0

    for _, row in lineup_df.iterrows():
        cards = row.get("cards", []) or []
        if not cards:
            continue

        player_name  = row["player_name"]
        nationality  = row.get("country", team_name)
        player_id    = _get_or_create_player(conn, player_name, nationality)

        for card in cards:
            card_type_raw = card.get("card_type", "")
            card_type     = CARD_MAP.get(card_type_raw)
            if not card_type:
                continue
            minute_str = card.get("time", "0:0")
            try:
                minute = int(minute_str.split(":")[0])
            except (ValueError, AttributeError):
                minute = 0

            conn.execute("""
                INSERT OR IGNORE INTO card_event
                  (match_id, player_id, team_id, minute, card_type)
                VALUES (?,?,?,?,?)
            """, (match_id, player_id, team_id, minute, card_type))
            loaded += 1

    return loaded


# ── fő betöltő ────────────────────────────────────────────────────────────────

def load_statsbomb(year: int, conn: sqlite3.Connection) -> dict:
    season_id = SEASONS.get(year)
    if not season_id:
        print(f"[sb] {year} nincs konfigurálva")
        return {}

    print(f"[sb] {year} – meccsek lekérése...")
    matches = sb.matches(competition_id=43, season_id=season_id)
    stats   = {"lineup": 0, "cards": 0, "xg_updated": 0, "no_match": 0}

    for _, m in matches.iterrows():
        home_name = m["home_team"]
        away_name = m["away_team"]
        date      = str(m["match_date"])
        sb_match_id = m["match_id"]

        # DB-beli meccs megkeresése
        match_id = _find_match(conn, year, home_name, away_name, date)
        if match_id is None:
            # Próbáljuk fordítva (néha home/away felcserélve)
            match_id = _find_match(conn, year, away_name, home_name, date)
        if match_id is None:
            stats["no_match"] += 1
            continue

        home_id = _resolve_team_id(conn, home_name)
        away_id = _resolve_team_id(conn, away_name)
        if not home_id or not away_id:
            stats["no_match"] += 1
            continue

        # Lineup
        try:
            lineups = sb.lineups(match_id=sb_match_id)
            for team_name, team_id in [(home_name, home_id), (away_name, away_id)]:
                if team_name in lineups:
                    n = _load_lineup(conn, match_id, team_id,
                                     team_name, lineups[team_name])
                    stats["lineup"] += n
                    c = _load_cards(conn, match_id, team_id,
                                    team_name, lineups[team_name])
                    stats["cards"] += c
        except Exception as e:
            print(f"  [warn] lineup hiba {home_name} vs {away_name}: {e}")

        # Shotek + xG
        try:
            events = sb.events(match_id=sb_match_id)
            shots  = events[events["type"] == "Shot"] if "type" in events.columns else None
            if shots is not None and not shots.empty:
                _update_xg(conn, match_id, shots)
                stats["xg_updated"] += len(shots[shots["shot_outcome"] == "Goal"])
        except Exception as e:
            print(f"  [warn] events hiba {home_name} vs {away_name}: {e}")

    return stats


def run(years: list[int], conn: sqlite3.Connection) -> None:
    for year in years:
        print(f"\n[sb] === {year} ===")
        with conn:
            stats = load_statsbomb(year, conn)
        print(
            f"[sb] {year} kész – "
            f"{stats.get('lineup',0)} lineup bejegyzés, "
            f"{stats.get('cards',0)} lap, "
            f"{stats.get('xg_updated',0)} xG frissítve, "
            f"{stats.get('no_match',0)} meccs nem találva a DB-ben"
        )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db import get_connection, init_db

    years = [int(y) for y in sys.argv[1:]] if len(sys.argv) > 1 else [2018, 2022]
    init_db()
    conn = get_connection()
    run(years, conn)
    conn.close()