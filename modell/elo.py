"""
features/elo.py

ELO számítás az összes meccsre időrendben.

Logika:
  1. Minden csapat kap egy kezdő ELO-t (1500.0)
  2. Végigmegyünk az összes meccsen dátum szerint
  3. Meccs előtt: eltároljuk az ELO-t (elo_diff_pre a match táblába)
  4. Meccs után: frissítjük az ELO-t a K-faktor alapján
  5. Minden frissítés bekerül az elo_log táblába (audit trail)

K-faktor torna típusonként:
  - Barátságos:        K=20
  - Selejtező:         K=25
  - Kontinentális VB: K=35
  - FIFA VB:           K=60  (legnagyobb tét)

Várható eredmény (Elo-képlet):
  E_a = 1 / (1 + 10^((R_b - R_a) / 400))

Tényleges eredmény:
  Győzelem: 1.0 | Döntetlen: 0.5 | Vereség: 0.0

Futtatás:
  python features/elo.py           # teljes újraszámítás
  python features/elo.py --reset   # törli az elo_log-ot és újraindul
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection

DEFAULT_ELO = 1500.0

# K-faktor meccs típusonként
K_FACTORS: dict[str, float] = {
    "FIFA World Cup":                           60.0,
    "FIFA World Cup qualification":             25.0,
    "Friendly":                                 20.0,
    "UEFA Euro":                                40.0,
    "Copa América":                             40.0,
    "Africa Cup of Nations":                    35.0,
    "AFC Asian Cup":                            35.0,
    "CONCACAF Gold Cup":                        35.0,
    "UEFA Nations League":                      30.0,
    "Confederations Cup":                       40.0,
    # default
    "_default":                                 25.0,
}

# Torna-szintű stage → K szorzó (knockout > csoportkör)
STAGE_MULTIPLIER: dict[str, float] = {
    "Final":          1.25,
    "Semi-finals":    1.15,
    "Quarter-finals": 1.10,
    "Round of 16":    1.05,
    "Group":          1.00,  # default
    "Qualifier":      1.00,
}


def _k_factor(tournament_name: str, stage: str) -> float:
    base_k = K_FACTORS.get(tournament_name, K_FACTORS["_default"])

    mult = 1.0
    for key, val in STAGE_MULTIPLIER.items():
        if key.lower() in stage.lower():
            mult = val
            break

    return base_k * mult


def _expected(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


def _actual(home_score: int | None, away_score: int | None) -> tuple[float, float]:
    """Visszaadja (home_result, away_result) ahol 1.0=győz, 0.5=döntetlen, 0.0=veszít."""
    if home_score is None or away_score is None:
        return (None, None)
    if home_score > away_score:
        return (1.0, 0.0)
    if home_score == away_score:
        return (0.5, 0.5)
    return (0.0, 1.0)


def compute_elo(conn: sqlite3.Connection, reset: bool = False) -> dict:
    """
    Teljes ELO számítás.
    reset=True → törli az elo_log-ot és a match.elo_diff_pre értékeket.
    """
    if reset:
        conn.execute("DELETE FROM elo_log")
        conn.execute("UPDATE match SET elo_diff_pre = NULL")
        conn.execute("UPDATE team_tournament_stat SET elo_pre=NULL, elo_post=NULL")
        print("[elo] Reset kész – újraszámítás indul")

    # elo_diff_pre oszlop biztosan létezik (séma már tartalmazza)
    elo: dict[int, float] = {}  # team_id → current ELO

    # Meccsek időrendben, torna névvel együtt
    matches = conn.execute("""
        SELECT
            m.id,
            m.match_date,
            m.home_team_id,
            m.away_team_id,
            m.home_score,
            m.away_score,
            m.stage,
            t.year AS tournament_year,
            CASE
                WHEN t.year IN (2002,2006,2010,2014,2018,2022,2026)
                     AND m.stage NOT LIKE '%ualif%'
                THEN 'FIFA World Cup'
                WHEN m.stage LIKE '%ualif%'
                THEN 'FIFA World Cup qualification'
                ELSE 'Friendly'
            END AS tournament_type
        FROM match m
        JOIN tournament t ON t.id = m.tournament_id
        WHERE m.match_date IS NOT NULL
        ORDER BY m.match_date ASC, m.id ASC
    """).fetchall()

    stats = {"processed": 0, "skipped_no_result": 0, "log_entries": 0}

    for row in matches:
        match_id  = row["id"]
        home_id   = row["home_team_id"]
        away_id   = row["away_team_id"]
        home_sc   = row["home_score"]
        away_sc   = row["away_score"]
        stage     = row["stage"] or ""
        t_type    = row["tournament_type"]

        # Kezdő ELO ha még nem szerepelt
        elo.setdefault(home_id, DEFAULT_ELO)
        elo.setdefault(away_id, DEFAULT_ELO)

        elo_home = elo[home_id]
        elo_away = elo[away_id]
        diff_pre = elo_home - elo_away

        # elo_diff_pre mentés a meccs táblába
        conn.execute(
            "UPDATE match SET elo_diff_pre=? WHERE id=?",
            (round(diff_pre, 2), match_id)
        )

        # Ha nincs eredmény (jövőbeli meccs), ELO-t nem frissítjük
        home_result, away_result = _actual(home_sc, away_sc)
        if home_result is None:
            stats["skipped_no_result"] += 1
            continue

        k = _k_factor(t_type, stage)

        exp_home = _expected(elo_home, elo_away)
        exp_away = _expected(elo_away, elo_home)

        new_elo_home = elo_home + k * (home_result - exp_home)
        new_elo_away = elo_away + k * (away_result - exp_away)

        # Log bejegyzés
        conn.executemany(
            """INSERT INTO elo_log(team_id, match_id, elo_before, elo_after, k_factor)
               VALUES (?,?,?,?,?)""",
            [
                (home_id, match_id, round(elo_home, 2), round(new_elo_home, 2), k),
                (away_id, match_id, round(elo_away, 2), round(new_elo_away, 2), k),
            ]
        )

        elo[home_id] = new_elo_home
        elo[away_id] = new_elo_away
        stats["processed"]  += 1
        stats["log_entries"] += 2

    # team_tournament_stat frissítése pre/post ELO-val
    # Minden tornához megkeressük az első és utolsó meccs ELO-ját
    _update_tournament_elo(conn, elo)

    return stats


def _update_tournament_elo(conn: sqlite3.Connection,
                             current_elo: dict[int, float]) -> None:
    """
    Frissíti a team_tournament_stat.elo_pre és elo_post mezőit.
    elo_pre = az adott csapat ELO-ja a torna első meccse ELŐTT
    elo_post = a torna utolsó meccse UTÁN (= jelenlegi ha a torna véget ért)
    """
    tts_rows = conn.execute("""
        SELECT tts.id, tts.team_id, t.year
        FROM team_tournament_stat tts
        JOIN tournament t ON t.id = tts.tournament_id
    """).fetchall()

    for row in tts_rows:
        team_id = row["team_id"]
        year    = row["year"]

        # Első meccs ELO-ja a tornán (az elo_log első bejegyzése erre a csapatra/tornára)
        first_elo = conn.execute("""
            SELECT el.elo_before
            FROM elo_log el
            JOIN match m ON m.id = el.match_id
            JOIN tournament t ON t.id = m.tournament_id
            WHERE el.team_id = ? AND t.year = ?
            ORDER BY m.match_date ASC, m.id ASC
            LIMIT 1
        """, (team_id, year)).fetchone()

        # Utolsó meccs utáni ELO
        last_elo = conn.execute("""
            SELECT el.elo_after
            FROM elo_log el
            JOIN match m ON m.id = el.match_id
            JOIN tournament t ON t.id = m.tournament_id
            WHERE el.team_id = ? AND t.year = ?
            ORDER BY m.match_date DESC, m.id DESC
            LIMIT 1
        """, (team_id, year)).fetchone()

        elo_pre  = first_elo["elo_before"] if first_elo else current_elo.get(team_id)
        elo_post = last_elo["elo_after"]   if last_elo  else None

        conn.execute("""
            UPDATE team_tournament_stat
            SET elo_pre=?, elo_post=?
            WHERE id=?
        """, (
            round(elo_pre, 2)  if elo_pre  else None,
            round(elo_post, 2) if elo_post else None,
            row["id"]
        ))


def top_elo(conn: sqlite3.Connection, n: int = 20) -> None:
    """Kiírja a jelenlegi ELO top N csapatot."""
    rows = conn.execute("""
        SELECT t.name, el.elo_after AS elo, el.logged_at
        FROM elo_log el
        JOIN team t ON t.id = el.team_id
        WHERE el.id = (
            SELECT id FROM elo_log
            WHERE team_id = el.team_id
            ORDER BY logged_at DESC, id DESC
            LIMIT 1
        )
        ORDER BY elo DESC
        LIMIT ?
    """, (n,)).fetchall()

    print(f"\n{'Rang':<5} {'Csapat':<25} {'ELO':>7}")
    print("-" * 40)
    for i, row in enumerate(rows, 1):
        print(f"{i:<5} {row['name']:<25} {row['elo']:>7.1f}")


if __name__ == "__main__":
    reset = "--reset" in sys.argv

    conn = get_connection()

    print("[elo] Számítás indul...")
    with conn:
        stats = compute_elo(conn, reset=reset)

    print(
        f"[elo] Kész – {stats['processed']} meccs feldolgozva, "
        f"{stats['skipped_no_result']} kihagyva (nincs eredmény), "
        f"{stats['log_entries']} log bejegyzés"
    )

    top_elo(conn)
    conn.close()