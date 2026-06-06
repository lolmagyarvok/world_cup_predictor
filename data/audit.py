import sqlite3
from pathlib import Path

def audit_database(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("\n" + "="*50)
    print(" 📊 ADATBÁZIS AUDIT JELENTÉS (TELJES SÉMA)")
    print("="*50)

    # 1. TÁBLÁK MÉRETE (Minden tábla a sémából)
    tables = [
        "tournament", "team", "player", 
        "team_tournament_stat", "player_tournament_stat", 
        "match", "match_lineup", 
        "goal_event", "card_event", "penalty_shootout", "elo_log"
    ]
    print("\n📌 1. TÁBLÁK REKORDSZÁMA (Mennyi adatunk van?)")
    print("-" * 50)
    for table in tables:
        try:
            count = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f" - {table.ljust(25)}: {count:>6} sor")
        except sqlite3.OperationalError:
            print(f" - {table.ljust(25)}: [HIÁNYZIK A TÁBLA!]")

    # 2. MECCSEK ELOSZLÁSA
    print("\n📌 2. MECCSEK SZÁMA TORNÁNKÉNT (Top 5 legfrissebb)")
    print("-" * 50)
    query = """
        SELECT t.year, t.host_country, COUNT(m.id) as match_count
        FROM tournament t
        LEFT JOIN match m ON t.id = m.tournament_id
        GROUP BY t.id
        ORDER BY t.year DESC
        LIMIT 5
    """
    for row in cur.execute(query):
        print(f" - {row['year']} ({row['host_country']}): {row['match_count']} meccs")

    # 3. ADATMINŐSÉG ÉS HIÁNYZÓ ÉRTÉKEK
    print("\n📌 3. ADATMINŐSÉG ÉS NULL (Sanity Checks)")
    print("-" * 50)
    
    # 3.1. Meccsek eredmény nélkül
    null_scores = cur.execute("SELECT COUNT(*) FROM match WHERE home_score IS NULL OR away_score IS NULL").fetchone()[0]
    print(f" - Meccsek hiányzó eredménnyel: {null_scores}")

    # 3.2. "Szellem" csapatok (akik sose játszottak)
    ghost_teams = cur.execute("""
        SELECT COUNT(*) FROM team 
        WHERE id NOT IN (SELECT home_team_id FROM match) 
        AND id NOT IN (SELECT away_team_id FROM match)
    """).fetchone()[0]
    print(f" - Csapatok lejátszott meccs nélkül: {ghost_teams}")

    # 3.3. Játékosok, akik nincsenek hozzárendelve csapathoz egyetlen tornán sem
    ghost_players = cur.execute("""
        SELECT COUNT(*) FROM player 
        WHERE id NOT IN (SELECT player_id FROM player_tournament_stat)
    """).fetchone()[0]
    print(f" - Játékosok torna-statisztika nélkül: {ghost_players}")

    # 4. MÉLY ADATOK FEDETTSÉGE (Lineups, Events, ELO)
    print("\n📌 4. RÉSZLETES ADATOK FEDETTSÉGE")
    print("-" * 50)

    # 4.1. Felállások (Lineups) fedettsége
    total_matches = cur.execute("SELECT COUNT(*) FROM match").fetchone()[0]
    matches_with_lineup = cur.execute("SELECT COUNT(DISTINCT match_id) FROM match_lineup").fetchone()[0]
    lineup_pct = (matches_with_lineup / total_matches * 100) if total_matches > 0 else 0
    print(f" - Meccsek kezdőcsapat adatokkal: {matches_with_lineup} / {total_matches} ({lineup_pct:.1f}%)")

    # 4.2. Gól események (Goal events)
    matches_with_goals = cur.execute("SELECT COUNT(DISTINCT match_id) FROM goal_event").fetchone()[0]
    goal_event_pct = (matches_with_goals / total_matches * 100) if total_matches > 0 else 0
    print(f" - Meccsek részletes gól eseményekkel: {matches_with_goals} / {total_matches} ({goal_event_pct:.1f}%)")

    # 4.3. ELO Log ellenőrzés
    elo_logs = cur.execute("SELECT COUNT(*) FROM elo_log").fetchone()[0]
    teams_with_elo = cur.execute("SELECT COUNT(DISTINCT team_id) FROM elo_log").fetchone()[0]
    print(f" - Generált ELO log bejegyzések: {elo_logs} (Érintett csapatok: {teams_with_elo})")

    # 4.4 Hiányzó FIFA Ranging / ELO a torna statisztikákban
    missing_pre_stats = cur.execute("""
        SELECT COUNT(*) FROM team_tournament_stat 
        WHERE fifa_rank_pre IS NULL OR elo_pre = 1500.0
    """).fetchone()[0]
    print(f" - Torna nevezések hiányzó (vagy alapértelmezett) Pre-ELO/FIFA adattal: {missing_pre_stats}")

    print("\n" + "="*50 + "\n")
    conn.close()

if __name__ == "__main__":
    # Ha máshol van az adatbázis, itt írd át az elérési utat!
    db_file = "database/worldcup_database.db" 
    
    if Path(db_file).exists():
        audit_database(db_file)
    else:
        print(f"❌ Hiba: Nem találom az adatbázist itt: {db_file}")