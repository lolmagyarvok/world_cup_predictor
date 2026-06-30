# check_state.py
import sqlite3
conn = sqlite3.connect('database/worldcup_database.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Prediction statuses
print('=== DAILY PREDICTIONS STATUSZOK ===')
c.execute('SELECT status, COUNT(*) FROM daily_predictions GROUP BY status')
for r in c.fetchall():
    print(f'  {r["status"]}: {r["COUNT(*)"]}')

print()
print('=== PREDIKCIOK LISTAJA ===')
c.execute('''SELECT dp.id, dp.match_date, t1.name as home, t2.name as away,
    dp.status, dp.actual_winner_id, dp.predicted_winner_id
FROM daily_predictions dp
JOIN team t1 ON dp.home_team_id = t1.id
JOIN team t2 ON dp.away_team_id = t2.id
ORDER BY dp.match_date, dp.id''')
for r in c.fetchall():
    print(f'  id={r["id"]} {r["match_date"]}: {r["home"]} vs {r["away"]} [status={r["status"]}]')

print()
print('=== MATCH TABLA (Group stage, van eredmeny) ===')
c.execute('''SELECT m.id, m.match_date, t1.name as home, t2.name as away, m.home_score, m.away_score
FROM match m JOIN team t1 ON m.home_team_id = t1.id JOIN team t2 ON m.away_team_id = t2.id
JOIN tournament t ON m.tournament_id = t.id
WHERE t.year = 2026 AND m.stage LIKE 'Group%%' AND m.home_score IS NOT NULL
ORDER BY m.match_date''')
rows = c.fetchall()
print(f'  Osszesen: {len(rows)}')
for r in rows[:10]:
    print(f'  {r["match_date"]}: {r["home"]} {r["home_score"]}-{r["away_score"]} {r["away"]}')

print()
print('=== MATCH TABLA (R32, van eredmeny) ===')
c.execute('''SELECT m.id, m.match_date, t1.name as home, t2.name as away, m.home_score, m.away_score
FROM match m JOIN team t1 ON m.home_team_id = t1.id JOIN team t2 ON m.away_team_id = t2.id
JOIN tournament t ON m.tournament_id = t.id
WHERE t.year = 2026 AND m.stage = 'Round of 32' AND m.home_score IS NOT NULL
ORDER BY m.match_date''')
rows = c.fetchall()
print(f'  Osszesen: {len(rows)}')
for r in rows:
    print(f'  {r["match_date"]}: {r["home"]} {r["home_score"]}-{r["away_score"]} {r["away"]}')

print()
print('=== MATCH TABLA (R32, NINCS eredmeny) ===')
c.execute('''SELECT m.id, m.match_date, t1.name as home, t2.name as away
FROM match m JOIN team t1 ON m.home_team_id = t1.id JOIN team t2 ON m.away_team_id = t2.id
JOIN tournament t ON m.tournament_id = t.id
WHERE t.year = 2026 AND m.stage = 'Round of 32' AND m.home_score IS NULL
ORDER BY m.match_date''')
rows = c.fetchall()
print(f'  Osszesen: {len(rows)}')
for r in rows:
    print(f'  {r["match_date"]}: {r["home"]} vs {r["away"]}')

conn.close()
