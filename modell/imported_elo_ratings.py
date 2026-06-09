"""
features/import_eloratings.py

Előre kiszámolt, valós 2026-os World Football Elo Ratings adatok importálása.
Ezek a pontszámok tartalmazzák a gólkülönbség, hazai pálya és a meccs-súlyozások
összes történelmi hatását, egészen 2026 nyaráig.

Futtatás:
  python features/import_eloratings.py
"""

import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection

# 2026-os valós (eloratings.net alapú) ELO pontszámok a VB-re esélyes csapatoknak
ELO_2026 = {
    "Spain": 2165, "Argentina": 2150, "France": 2081, "Colombia": 2069,
    "England": 2020, "Uruguay": 2002, "Portugal": 1984, "Brazil": 1975,
    "Netherlands": 1961, "Croatia": 1930, "Germany": 1923, "Norway": 1912,
    "Turkey": 1902, "Denmark": 1869, "Belgium": 1867, "Italy": 1856,
    "Japan": 1845, "Morocco": 1820, "Switzerland": 1815, "Ecuador": 1810,
    "Iran": 1795, "USA": 1780, "Senegal": 1775, "Serbia": 1769,
    "Mexico": 1765, "South Korea": 1755, "Greece": 1752, "Austria": 1745,
    "Panama": 1740, "Australia": 1735, "Czech Republic": 1726, "Venezuela": 1720,
    "Canada": 1715, "Wales": 1698, "Ivory Coast": 1690, "Algeria": 1685,
    "Egypt": 1680, "Nigeria": 1675, "Mali": 1670, "Costa Rica": 1661,
    "Paraguay": 1660, "Cameroon": 1650, "Saudi Arabia": 1640, "Tunisia": 1635,
    "Peru": 1630, "Chile": 1625, "Poland": 1620, "Scotland": 1610,
    "Qatar": 1605, "Uzbekistan": 1600, "Jamaica": 1590, "Bolivia": 1550,
    "South Africa": 1545, "Honduras": 1530, "New Zealand": 1520
}

def import_static_elo(conn: sqlite3.Connection):
    cursor = conn.cursor()
    
    # Létrehozunk egy dedikált táblát a statikus Elo értékeknek
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS static_elo (
            team_id INTEGER PRIMARY KEY,
            elo_rating REAL NOT NULL,
            FOREIGN KEY(team_id) REFERENCES team(id)
        )
    """)
    cursor.execute("DELETE FROM static_elo") # Töröljük a régit frissítés esetén
    
    # Nevek párosítása a DB-ben lévő team_id-kkal
    teams_in_db = cursor.execute("SELECT id, name FROM team").fetchall()
    name_to_id = {row["name"]: row["id"] for row in teams_in_db}
    
    inserted = 0
    missing = []
    
    for team_name, elo_val in ELO_2026.items():
        team_id = name_to_id.get(team_name)
        if team_id:
            cursor.execute(
                "INSERT INTO static_elo (team_id, elo_rating) VALUES (?, ?)", 
                (team_id, float(elo_val))
            )
            inserted += 1
        else:
            missing.append(team_name)
            
    conn.commit()
    print(f"✅ {inserted} valós ELO pontszám importálva a 'static_elo' táblába.")
    if missing:
        print(f"⚠️ A következő csapatokat nem találtam a DB-ben (lehet, hogy más a nevük): {missing}")

if __name__ == "__main__":
    conn = get_connection()
    import_static_elo(conn)
    conn.close()