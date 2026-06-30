#!/usr/bin/env python3
"""Check current database state for 2026 WC predictions/matches."""
import sqlite3, json

conn = sqlite3.connect(r'c:\Users\User\vb predictor\database\worldcup_database.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

result = {}

# Predictions
c.execute('SELECT status, COUNT(*) as cnt FROM daily_predictions GROUP BY status')
result['predictions_by_status'] = {r['status']: r['cnt'] for r in c.fetchall()}

c.execute('''SELECT dp.id, dp.match_date, t1.name as home, t2.name as away,
    dp.status
FROM daily_predictions dp JOIN team t1 ON dp.home_team_id = t1.id JOIN team t2 ON dp.away_team_id = t2.id
ORDER BY dp.match_date, dp.id''')
result['predictions'] = [dict(r) for r in c.fetchall()]

# Group stage matches with scores
c.execute('''SELECT m.id, m.match_date, t1.name as home, t2.name as away, m.home_score, m.away_score
FROM match m JOIN team t1 ON m.home_team_id = t1.id JOIN team t2 ON m.away_team_id = t2.id
JOIN tournament t ON m.tournament_id = t.id
WHERE t.year = 2026 AND m.stage LIKE 'Group%%' AND m.home_score IS NOT NULL
ORDER BY m.match_date''')
result['group_matches_with_scores'] = [dict(r) for r in c.fetchall()]

# R32 matches
c.execute('''SELECT m.id, m.match_date, t1.name as home, t2.name as away, m.home_score, m.away_score
FROM match m JOIN team t1 ON m.home_team_id = t1.id JOIN team t2 ON m.away_team_id = t2.id
JOIN tournament t ON m.tournament_id = t.id
WHERE t.year = 2026 AND m.stage = 'Round of 32'
ORDER BY m.match_date''')
result['r32_matches'] = [dict(r) for r in c.fetchall()]

conn.close()

with open(r'c:\Users\User\vb predictor\scripts\state_check.json', 'w') as f:
    json.dump(result, f, indent=2, default=str)

print('Result saved to state_check.json')
