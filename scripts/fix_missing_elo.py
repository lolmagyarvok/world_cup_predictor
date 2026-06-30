"""
Fix missing ELO entries for 9 teams and recalculate group stage ELO.
"""
import sqlite3

conn = sqlite3.connect('database/worldcup_database.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

# 1. Find and add base ELO for missing teams
missing = c.execute("""
    SELECT DISTINCT t.id, t.name
    FROM match m
    JOIN team t ON t.id IN (m.home_team_id, m.away_team_id)
    JOIN tournament tn ON m.tournament_id = tn.id
    WHERE tn.year = 2026 AND m.stage LIKE 'Group%'
      AND NOT EXISTS (SELECT 1 FROM static_elo se WHERE se.team_id = t.id)
    ORDER BY t.name
""").fetchall()

print(f"=== Hianyzo ELO-val rendelkezo csapatok: {len(missing)} ===")
for tid, name in missing:
    c.execute("INSERT OR IGNORE INTO static_elo (team_id, elo_rating) VALUES (?, 1500.0)", (tid,))
    print(f"  Alap ELO (1500) hozzaadva: {name} (id={tid})")
conn.commit()

# 2. Recalculate all group stage matches in chronological order
ELO_K = 40
matches = c.execute("""
    SELECT m.id, m.home_team_id, m.away_team_id, m.home_score, m.away_score,
           t1.name as h, t2.name as a
    FROM match m
    JOIN tournament t ON m.tournament_id = t.id
    WHERE t.year = 2026 AND m.stage LIKE 'Group%' AND m.home_score IS NOT NULL
    ORDER BY m.match_date, m.id
""").fetchall()

print(f"\n=== Csoportkori meccsek feldolgozasa: {len(matches)} ===")
updated = 0
skipped = 0
for m in matches:
    home_id, away_id = m["home_team_id"], m["away_team_id"]
    hg, ag = m["home_score"], m["away_score"]

    h_elo = c.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (home_id,)).fetchone()
    a_elo = c.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (away_id,)).fetchone()
    if not h_elo or not a_elo:
        print(f"  Kihagyva (megerint nincs ELO): {m['h']} vs {m['a']}")
        skipped += 1
        continue

    r_h, r_a = h_elo["elo_rating"], a_elo["elo_rating"]
    e_h = 1.0 / (1.0 + 10**((r_a - r_h) / 400.0))

    if hg > ag: s_h = 1.0
    elif hg == ag: s_h = 0.5
    else: s_h = 0.0

    new_h = r_h + ELO_K * (s_h - e_h)
    new_a = r_a + ELO_K * (1.0 - s_h - (1.0 - e_h))

    c.execute("UPDATE static_elo SET elo_rating=? WHERE team_id=?", (new_h, home_id))
    c.execute("UPDATE static_elo SET elo_rating=? WHERE team_id=?", (new_a, away_id))
    updated += 1

conn.commit()
print(f"\n  Frissitve: {updated} meccs")
print(f"  Kihagyva: {skipped} meccs")

# 3. Summary
print(f"\n=== TOP 10 ELO ===")
for r in c.execute("""
    SELECT t.name, se.elo_rating
    FROM static_elo se
    JOIN team t ON se.team_id = t.id
    ORDER BY se.elo_rating DESC LIMIT 10
""").fetchall():
    print(f"  {r['name']}: {r['elo_rating']:.0f}")

print(f"\n=== ELO statisztika ===")
count = c.execute("SELECT COUNT(*) as cnt FROM static_elo").fetchone()["cnt"]
avg = c.execute("SELECT AVG(elo_rating) as avg FROM static_elo").fetchone()["avg"]
print(f"  Osszes ELO bejegyzes: {count}")
print(f"  Atlag ELO: {avg:.0f}")

print(f"\n=== PREDIKCIO STATUSZOK ===")
for r in c.execute("SELECT status, COUNT(*) as cnt FROM daily_predictions GROUP BY status").fetchall():
    print(f"  {r['status']}: {r['cnt']}")

conn.close()
print(f"\n[+] fix_missing_elo.py kesz.")
