"""
fix_2026_data.py

Tisztitja es ujratolti a 2026 VB adatokat a friss openfootball JSON-bol.
"""

import os, sys
from pathlib import Path
if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try: sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError: pass

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection

# Torlendo duplikalt/placeholder csapat ID-k
TEAMS_TO_DELETE = [
    213, 217,
    226, 227, 228, 229, 230, 231,
]

# Csapat ID atiranyitasok: torlendo -> megtartando
TEAM_ID_REMAP = {213: 32, 217: 59}

def step1_remap_teams(conn):
    cur = conn.cursor()
    for delete_id, keep_id in TEAM_ID_REMAP.items():
        for table, col in [
            ("daily_predictions", "home_team_id"),
            ("daily_predictions", "away_team_id"),
            ("daily_predictions", "predicted_winner_id"),
            ("daily_predictions", "actual_winner_id"),
            ("match", "home_team_id"),
            ("match", "away_team_id"),
            ("elo_log", "team_id"),
            ("static_elo", "team_id"),
            ("team_tournament_stat", "team_id"),
            ("player_tournament_stat", "team_id"),
        ]:
            cnt = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (delete_id,)).fetchone()[0]
            if cnt:
                cur.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (keep_id, delete_id))
                print(f"  [->] {table}.{col}: {cnt} db atirva {delete_id}->{keep_id}")
    conn.commit()

def step2_delete_placeholder_matches(conn):
    """Torol minden meccset ami placeholder csapatokra hivatkozik."""
    cur = conn.cursor()
    placeholder_ids = [tid for tid in TEAMS_TO_DELETE if tid not in TEAM_ID_REMAP]
    for tid in placeholder_ids:
        ids = [r["id"] for r in cur.execute(
            "SELECT id FROM match WHERE home_team_id=? OR away_team_id=?", (tid, tid)
        ).fetchall()]
        if ids:
            id_list = ",".join("?" * len(ids))
            for et in ["goal_event", "card_event", "penalty_shootout", "match_lineup"]:
                cur.execute(f"DELETE FROM {et} WHERE match_id IN ({id_list})", ids)
            cur.execute(f"DELETE FROM match WHERE id IN ({id_list})", ids)
            print(f"  [x] {len(ids)} placeholder meccs torolve (team_id={tid})")
    conn.commit()

def step3_delete_tournament_matches(conn):
    """Torol minden 2026 VB torna meccset (nem selejtezot)."""
    cur = conn.cursor()
    matches = cur.execute("""
        SELECT m.id FROM match m
        JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage NOT LIKE 'Qualifier' AND m.stage NOT LIKE '%qualif%'
    """).fetchall()
    if not matches:
        print("  Nincsenek 2026 VB meccsek.")
        return
    ids = [m["id"] for m in matches]
    id_list = ",".join("?" * len(ids))
    for et in ["goal_event", "card_event", "penalty_shootout", "match_lineup"]:
        cnt = cur.execute(f"DELETE FROM {et} WHERE match_id IN ({id_list})", ids).rowcount
        if cnt: print(f"  [x] {cnt} {et} rekord torolve")
    cur.execute(f"DELETE FROM match WHERE id IN ({id_list})", ids)
    print(f"  [x] {len(ids)} VB meccs torolve")
    conn.commit()

FK_TABLES = [
    ("daily_predictions", ["home_team_id", "away_team_id", "predicted_winner_id", "actual_winner_id"]),
    ("match", ["home_team_id", "away_team_id"]),
    ("elo_log", ["team_id"]),
    ("static_elo", ["team_id"]),
    ("team_tournament_stat", ["team_id"]),
    ("player_tournament_stat", ["team_id"]),
    ("goal_event", ["team_id"]),
    ("card_event", ["team_id"]),
    ("penalty_shootout", ["team_id"]),
    ("match_lineup", ["team_id"]),
]

def delete_all_references(conn, team_ids):
    """Torol minden hivatkozast a megadott csapat ID-kra, hogy torolheto legyen a csapat."""
    cur = conn.cursor()
    for tid in team_ids:
        for table, columns in FK_TABLES:
            for col in columns:
                cnt = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (tid,)).fetchone()[0]
                if cnt > 0:
                    cur.execute(f"DELETE FROM {table} WHERE {col}=?", (tid,))
                    print(f"  [x] {cnt} {table}.{col} rekord torolve (team_id={tid})")
    conn.commit()


def step4_delete_teams(conn):
    """Torli a duplikalt es placeholder csapatokat FK ellenorzes kikapcsolassal."""
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = OFF")
    for tid in TEAMS_TO_DELETE:
        name = cur.execute("SELECT name FROM team WHERE id=?", (tid,)).fetchone()
        if not name:
            continue
        cur.execute("DELETE FROM team WHERE id=?", (tid,))
        print(f"  [x] Csapat torolve: id={tid} '{name['name']}'")
    cur.execute("PRAGMA foreign_keys = ON")
    conn.commit()

def main():
    print("=" * 55)
    print("  2026 VB adatok tisztitasa es ujratoltese")
    print("=" * 55)

    conn = get_connection()

    print("\n1. Csapat ID atiranyitasok...")
    step1_remap_teams(conn)

    print("\n2. Placeholder meccsek torlese...")
    step2_delete_placeholder_matches(conn)

    print("\n3. 2026 VB torna meccsek torlese...")
    step3_delete_tournament_matches(conn)

    print("\n3b. Maradek FK hivatkozasok torlese (placeholder csapatokra)...")
    delete_all_references(conn, TEAMS_TO_DELETE)

    print("\n4. Duplikalt/placeholder csapatok torlese...")
    step4_delete_teams(conn)

    print("\n5. 2026 adatok ujratoltese openfootball JSON-bol...")
    from data.worldcup_json_loader import run as load_wc
    load_wc([2026], conn)

    print("\n6. Eredmenyek ellenorzese...")
    cur = conn.cursor()

    total = cur.execute("""
        SELECT COUNT(*) FROM match m JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage LIKE 'Group%'
    """).fetchone()[0]

    scored = cur.execute("""
        SELECT COUNT(*) FROM match m JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage LIKE 'Group%' AND m.home_score IS NOT NULL
    """).fetchone()[0]

    r32 = cur.execute("""
        SELECT COUNT(*) FROM match m JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage = 'Round of 32'
    """).fetchone()[0]

    r32s = cur.execute("""
        SELECT COUNT(*) FROM match m JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage = 'Round of 32' AND m.home_score IS NOT NULL
    """).fetchone()[0]

    all_matches = cur.execute("""
        SELECT COUNT(*) FROM match m JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026
    """).fetchone()[0]

    print("")
    print(f"  Osszes 2026 meccs: {all_matches}")
    print(f"  Csoportkor: {total} (ebbol {scored}-nak van eredmenye)")
    print(f"  R32: {r32} (ebbol {r32s}-nak van eredmenye)")

    # R32 meccsek listaja
    print("\n  R32 meccsek:")
    for r in cur.execute("""
        SELECT m.match_date, t1.name as h, t2.name as a, m.home_score, m.away_score
        FROM match m JOIN team t1 ON m.home_team_id=t1.id JOIN team t2 ON m.away_team_id=t2.id
        JOIN tournament t ON m.tournament_id=t.id
        WHERE t.year=2026 AND m.stage='Round of 32' ORDER BY m.match_date
    """).fetchall():
        score = f"{r['home_score']}-{r['away_score']}" if r['home_score'] is not None else "?"
        print(f"    {r['match_date']}: {r['h']} vs {r['a']} ({score})")

    conn.close()
    print("\n[+] Tisztitas es ujratoltes kesz!")

if __name__ == "__main__":
    main()
