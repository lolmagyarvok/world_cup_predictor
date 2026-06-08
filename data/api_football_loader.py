"""
parsers/api_football_loader.py

API-Football.com adatok betöltése:
  - 2026 VB keretek (/players/squads)
  - Sérülések (/injuries)

Free tier: 100 request/nap. Ez a script cache-el (JSON fájlokba),
hogy ne pazarolja a kvótát újrafuttatáskor.

Beállítás:
  export API_FOOTBALL_KEY="your_key_here"
  python api_football_loader.py

Vagy:
  python api_football_loader.py --key YOUR_KEY

API-Football regisztráció: https://dashboard.api-football.com/register
  → Free plan: 100 req/nap, API key azonnal.

WC 2026 league_id = 1 (FIFA World Cup)
WC 2026 season   = 2026
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv


CACHE_DIR  = Path(__file__).parent.parent / "data" / "api_cache"
BASE_URL   = "https://v3.football.api-sports.io"
WC_LEAGUE  = 1
WC_SEASON  = 2022

# WC 2026 résztvevők – az API team ID-kat kézzel kell megadni,
# vagy /teams?league=1&season=2026 endpointból lekérni
# Ezt a script automatikusan tölti le az első futáskor.

POSITION_MAP = {
    "Goalkeeper": "GK",
    "Defender":   "DEF",
    "Midfielder": "MID",
    "Attacker":   "FWD",
}

load_dotenv()

def _get_key(key_arg: Optional[str] = None) -> str:
    key = key_arg or os.environ.get("API_FOOTBALL_KEY", "")
    if not key:
        print("""
[api] ❌  Nincs API kulcs!

Beállítás:
  export API_FOOTBALL_KEY='your_key_here'

Vagy add meg --key paraméterrel:
  python api_football_loader.py --key YOUR_KEY

Regisztráció (ingyenes):
  https://dashboard.api-football.com/register
""")
        sys.exit(1)
    return key


def _fetch(endpoint: str, params: dict, key: str,
           cache_name: Optional[str] = None) -> Optional[dict]:
    """API hívás cache-eléssel. Cache hit esetén nem fogyaszt kvótát."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if cache_name:
        cache_file = CACHE_DIR / f"{cache_name}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())

    query = "&".join(f"{k}={v}" for k, v in params.items())
    url   = f"{BASE_URL}/{endpoint}?{query}"

    req = urllib.request.Request(
        url,
        headers={
            "x-apisports-key": key,
            "User-Agent": "vb-predictor/1.0",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"  [warn] API hiba ({endpoint}): {e}")
        return None

    # Kvóta kijelzése
    remaining = data.get("paging", {})
    if "errors" in data and data["errors"]:
        print(f"  [warn] API error: {data['errors']}")
        return None

    if cache_name:
        cache_file.write_text(json.dumps(data))

    # Rate limit: 10 req/sec a free tierben
    time.sleep(0.15)
    return data


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_or_create_team(conn: sqlite3.Connection, name: str,
                         country: str = "Unknown") -> int:
    row = conn.execute("SELECT id FROM team WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    base = name[:3].upper().replace(" ", "")
    code = base
    i = 1
    while conn.execute("SELECT 1 FROM team WHERE fifa_code=?", (code,)).fetchone():
        code = f"{base}{i}"
        i += 1
    cur = conn.execute(
        "INSERT INTO team(name,fifa_code,continent) VALUES(?,?,'Unknown')",
        (name, code)
    )
    return cur.lastrowid


def _upsert_player(conn: sqlite3.Connection, api_player: dict,
                    nationality: str) -> int:
    name     = api_player.get("name", "").strip()
    position = POSITION_MAP.get(api_player.get("position", ""), "MID")
    dob      = api_player.get("birth", {}).get("date")  # "1994-06-10"
    age      = api_player.get("age")

    row = conn.execute(
        "SELECT id FROM player WHERE name=? AND nationality=?",
        (name, nationality)
    ).fetchone()

    if row:
        # Frissítjük a pozíciót és DOB-ot ha volt ismeretlen
        conn.execute("""
            UPDATE player SET
                position = CASE WHEN position = 'FWD' THEN ? ELSE position END,
                date_of_birth = COALESCE(date_of_birth, ?)
            WHERE id = ?
        """, (position, dob, row["id"]))
        return row["id"]

    cur = conn.execute(
        "INSERT INTO player(name, nationality, position, date_of_birth) VALUES(?,?,?,?)",
        (name, nationality, position, dob)
    )
    return cur.lastrowid


def _get_tournament_id(conn: sqlite3.Connection, year: int) -> Optional[int]:
    row = conn.execute("SELECT id FROM tournament WHERE year=?", (year,)).fetchone()
    return row["id"] if row else None


# ── keretek betöltése ─────────────────────────────────────────────────────────

def load_squads(conn: sqlite3.Connection, key: str) -> dict:
    """
    Lekéri az összes WC 2026-os csapat keretét és
    feltölti a player + player_tournament_stat táblákat.
    """
    stats = {"teams": 0, "players": 0}

    # Csapatok lekérése
    print("[api] WC 2026 csapatok lekérése...")
    teams_data = _fetch(
        "teams",
        {"league": WC_LEAGUE, "season": WC_SEASON},
        key,
        cache_name="wc2026_teams"
    )

    if not teams_data or not teams_data.get("response"):
        print("[api] Nem sikerült csapatokat lekérni. Ellenőrizd az API kulcsot és hogy a 2026-os WC elérhető-e.")
        return stats

    tournament_id = _get_tournament_id(conn, 2026)
    if tournament_id is None:
        print("[api] 2026-os torna nincs a DB-ben – futtasd előbb a load_all.py-t")
        return stats

    for team_entry in teams_data["response"]:
        api_team    = team_entry["team"]
        api_team_id = api_team["id"]
        team_name   = api_team["name"]
        country     = api_team.get("country", "Unknown")

        team_id = _get_or_create_team(conn, team_name, country)

        print(f"  [api] {team_name} kerete...", end=" ")

        squad_data = _fetch(
            "players/squads",
            {"team": api_team_id},
            key,
            cache_name=f"squad_{api_team_id}"
        )

        if not squad_data or not squad_data.get("response"):
            print("HIBA")
            continue

        player_count = 0
        for squad_entry in squad_data["response"]:
            for player_info in squad_entry.get("players", []):
                player_id = _upsert_player(conn, player_info, country)

                # player_tournament_stat – alapértelmezés, majd injury frissíti
                conn.execute("""
                    INSERT INTO player_tournament_stat
                      (player_id, tournament_id, team_id, is_injured, is_key_player)
                    VALUES (?,?,?,0,0)
                    ON CONFLICT(player_id, tournament_id) DO NOTHING
                """, (player_id, tournament_id, team_id))

                player_count += 1

        print(f"{player_count} játékos")
        stats["players"] += player_count
        stats["teams"]   += 1

    return stats


# ── sérülések betöltése ───────────────────────────────────────────────────────

def load_injuries(conn: sqlite3.Connection, key: str) -> dict:
    """
    Lekéri a WC 2026 sérüléslistát és frissíti
    a player_tournament_stat.is_injured mezőt.
    """
    print("\n[api] Sérülések lekérése...")
    stats = {"updated": 0}

    tournament_id = _get_tournament_id(conn, 2026)
    if tournament_id is None:
        return stats

    inj_data = _fetch(
        "injuries",
        {"league": WC_LEAGUE, "season": WC_SEASON},
        key,
        cache_name="wc2026_injuries"
    )

    if not inj_data or not inj_data.get("response"):
        print("[api] Nincs sérülés adat (lehet, hogy még nem indult el a torna)")
        return stats

    for entry in inj_data["response"]:
        player_name = entry.get("player", {}).get("name", "")
        team_name   = entry.get("team", {}).get("name", "")
        reason      = entry.get("player", {}).get("reason", "")

        # Severity becslése a reason alapján
        reason_lower = reason.lower()
        if any(w in reason_lower for w in ["torn", "fracture", "rupture", "break"]):
            severity = "severe"
        elif any(w in reason_lower for w in ["muscle", "strain", "hamstring", "knee"]):
            severity = "moderate"
        else:
            severity = "minor"

        conn.execute("""
            UPDATE player_tournament_stat SET
                is_injured       = 1,
                injury_severity  = ?
            WHERE tournament_id = ?
              AND player_id IN (SELECT id FROM player WHERE name = ?)
              AND team_id   IN (SELECT id FROM team   WHERE name = ?)
        """, (severity, tournament_id, player_name, team_name))

        if conn.execute("SELECT changes()").fetchone()[0] > 0:
            stats["updated"] += 1

    return stats


# ── cache törlés ──────────────────────────────────────────────────────────────

def clear_cache() -> None:
    """Töröld a cache-t ha friss adatot akarsz (pl. sérülés frissül)."""
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
        print(f"[api] Cache törölve: {CACHE_DIR}")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from db import get_connection, init_db

    key_arg = None
    if "--key" in sys.argv:
        idx = sys.argv.index("--key")
        key_arg = sys.argv[idx + 1]

    if "--clear-cache" in sys.argv:
        clear_cache()
        sys.exit(0)

    api_key = _get_key(key_arg)

    init_db()
    conn = get_connection()

    with conn:
        squad_stats = load_squads(conn, api_key)
        inj_stats   = load_injuries(conn, api_key)

    conn.close()

    print(f"""
[api] Kész:
  Csapatok:   {squad_stats['teams']}
  Játékosok:  {squad_stats['players']}
  Sérülések:  {inj_stats['updated']} frissítve
    """)