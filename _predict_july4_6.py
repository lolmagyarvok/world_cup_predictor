"""
Create predictions for July 4-6 matches using the XGBoost model, then evaluate.
Bypasses the home_score IS NULL check in daily_predictor since these are past matches.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from data.db import get_connection
from modell.pipeline import build_prediction_row, _build_elo_timeline, _build_form_cache
from modell.train import load_model, predict_proba
import numpy as np
from decimal import Decimal, ROUND_HALF_UP

conn = get_connection()

# Load model
print("Loading model...")
model, feature_names = load_model()
elo_tl = _build_elo_timeline(conn)
form_cache = _build_form_cache(conn)

# Get July 4-6 matches
DATES = ["2026-07-14", "2026-07-15", "2026-07-18", "2026-07-19"]

for pred_date in DATES:
    matches = conn.execute("""
        SELECT m.id, m.home_team_id, m.away_team_id, m.stage,
               h.name AS home_name, a.name AS away_name
        FROM match m
        JOIN team h ON h.id = m.home_team_id
        JOIN team a ON a.id = m.away_team_id
        JOIN tournament t ON t.id = m.tournament_id
        WHERE t.year = 2026
          AND m.match_date = ?
        ORDER BY m.match_date
    """, (pred_date,)).fetchall()

    if not matches:
        print(f"No matches for {pred_date}")
        continue

    print(f"\n=== {pred_date}: {len(matches)} matches ===")

    for m in matches:
        home_id = m["home_team_id"]
        away_id = m["away_team_id"]
        home_name = m["home_name"]
        away_name = m["away_name"]
        stage = m["stage"] or "Group stage"

        # Predict
        try:
            X = build_prediction_row(conn, home_id, away_id, stage, elo_tl, form_cache)
            raw = predict_proba(model, feature_names, X)[0]
            classes = list(model.classes_)
            idx_away = classes.index(0)
            idx_draw = classes.index(1)
            idx_home = classes.index(2)
            probs = np.array([raw[idx_away], raw[idx_draw], raw[idx_home]])
        except Exception as e:
            print(f"  Model error for {home_name} vs {away_name}: {e} - using ELO fallback")
            h_row = conn.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (home_id,)).fetchone()
            a_row = conn.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (away_id,)).fetchone()
            r_h = h_row["elo_rating"] if h_row else 1500.0
            r_a = a_row["elo_rating"] if a_row else 1500.0
            p_h = 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))
            p_d = 0.22
            p_a = max(0.01, 1 - p_h - p_d)
            arr = np.array([p_a, p_d, max(0.01, p_h - p_d / 2)])
            probs = arr / arr.sum()

        away_p, draw_p, home_p = probs
        best_idx = int(np.argmax(probs))

        if best_idx == 2:
            pred_winner_id = home_id
            target_odds_key = "home"
            pred_label = home_name
        elif best_idx == 0:
            pred_winner_id = away_id
            target_odds_key = "away"
            pred_label = away_name
        else:
            pred_winner_id = None
            target_odds_key = "draw"
            pred_label = "Döntetlen"

        # Default odds (no API available for past dates)
        odds = {"home": Decimal('2.00'), "draw": Decimal('3.10'), "away": Decimal('2.00')}
        target_odds = odds[target_odds_key]

        STAKE = Decimal('10.00')
        prob_val = Decimal(str(home_p if best_idx == 2 else (away_p if best_idx == 0 else draw_p)))
        ev = (STAKE * target_odds * prob_val) - STAKE

        # Check if already exists
        existing = conn.execute("""
            SELECT id FROM daily_predictions
            WHERE match_date=? AND home_team_id=? AND away_team_id=?
        """, (pred_date, home_id, away_id)).fetchone()

        if not existing:
            conn.execute("""
                INSERT INTO daily_predictions
                    (match_date, home_team_id, away_team_id,
                     predicted_winner_id,
                     odds_home, odds_draw, odds_away,
                     target_odds, stake, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                pred_date, home_id, away_id,
                pred_winner_id,
                float(odds["home"]), float(odds["draw"]), float(odds["away"]),
                float(target_odds), float(STAKE)
            ))
            tag = "[NEW]"
        else:
            tag = "[exists]"

        print(f"  {tag} {home_name} vs {away_name}")
        print(f"    Stage: {stage}")
        print(f"    Probs -> Home: {home_p:.1%}  Draw: {draw_p:.1%}  Away: {away_p:.1%}")
        print(f"    Tip: {pred_label}  |  Odds: {target_odds:.2f}  |  EV: ${ev:.2f}")

conn.commit()
print("\n✅ Predictions created for July 4-6!")
print("\nNow evaluating...")

# Now evaluate - set actual_winner_id from match table
for pred_date in DATES:
    pending = conn.execute("""
        SELECT dp.id, dp.home_team_id, dp.away_team_id,
               m.home_score, m.away_score
        FROM daily_predictions dp
        JOIN match m ON m.home_team_id = dp.home_team_id
            AND m.away_team_id = dp.away_team_id
            AND m.tournament_id = (SELECT id FROM tournament WHERE year=2026)
            AND m.match_date = dp.match_date
        WHERE dp.match_date = ? AND dp.status = 'PENDING'
          AND m.home_score IS NOT NULL
    """, (pred_date,)).fetchall()

    for row in pending:
        h, a = row["home_score"], row["away_score"]
        if h > a:
            winner_id = row["home_team_id"]
        elif a > h:
            winner_id = row["away_team_id"]
        else:
            winner_id = None
        conn.execute("UPDATE daily_predictions SET actual_winner_id = ?, status = 'READY_FOR_EVAL' WHERE id = ?",
                     (winner_id, row["id"]))

    print(f"  {pred_date}: {len(pending)} marked as READY_FOR_EVAL")

# Evaluate
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict

rows = conn.execute("""
    SELECT dp.id, dp.match_date, dp.stake, dp.target_odds,
           dp.predicted_winner_id, dp.actual_winner_id,
           th.name as home_name, ta.name as away_name,
           tp.name as pred_name, tact.name as actual_name
    FROM daily_predictions dp
    JOIN team th ON dp.home_team_id = th.id
    JOIN team ta ON dp.away_team_id = ta.id
    LEFT JOIN team tp ON dp.predicted_winner_id = tp.id
    LEFT JOIN team tact ON dp.actual_winner_id = tact.id
    WHERE dp.match_date IN ('2026-07-04','2026-07-05','2026-07-06')
      AND dp.status = 'READY_FOR_EVAL'
    ORDER BY dp.match_date, dp.id
""").fetchall()

by_date = defaultdict(list)
for r in rows:
    by_date[r["match_date"]].append(r)

total_correct = 0
total_staked = Decimal('0')
total_returned = Decimal('0')

for eval_date in sorted(by_date):
    pending = by_date[eval_date]
    correct = 0
    staked = Decimal('0')
    returned = Decimal('0')

    print(f"\n--- {eval_date} ---")
    for row in pending:
        stake = Decimal(str(row["stake"]))
        t_odds = Decimal(str(row["target_odds"]))
        pred_id = row["predicted_winner_id"]
        actual_id = row["actual_winner_id"]
        home_name = row["home_name"]
        away_name = row["away_name"]
        pred_str = row["pred_name"] if pred_id is not None else "Döntetlen"
        actual_str = row["actual_name"] if actual_id is not None else "Döntetlen"

        staked += stake
        is_correct = (pred_id is None and actual_id is None) or (pred_id is not None and pred_id == actual_id)

        if is_correct:
            correct += 1
            win = (stake * t_odds).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            returned += win
            result_mark = "HELYES"
        else:
            result_mark = "ROSSZ"

        print(f"  {home_name} vs {away_name}: Tip={pred_str}, Actual={actual_str} -> {result_mark}")

    total_correct += correct
    total_staked += staked
    total_returned += returned

    accuracy = (correct / len(pending)) * 100
    net = returned - staked
    daily_roi = (net / staked * 100) if staked > 0 else 0

    # Update daily_metrics
    conn.execute("""
        INSERT INTO daily_metrics
            (date, matches_evaluated, correct_predictions, daily_staked, daily_returned, cumulative_roi, accuracy)
        VALUES (?, ?, ?, ?, ?, 0, ?)
    """, (eval_date, len(pending), correct, float(staked), float(returned), accuracy))

    # Update status
    for row in pending:
        conn.execute("UPDATE daily_predictions SET status='EVALUATED' WHERE id=?", (row["id"],))

    print(f"  -> {correct}/{len(pending)} helyes, Staked=${staked:.2f}, Returned=${returned:.2f}, ROI={float(daily_roi):+.2f}%")

# Recalculate cumulative ROI for all dates
cum_staked = Decimal('0')
cum_returned = Decimal('0')
all_metrics = conn.execute("SELECT date, daily_staked, daily_returned FROM daily_metrics ORDER BY date").fetchall()
for m in all_metrics:
    cum_staked += Decimal(str(m["daily_staked"]))
    cum_returned += Decimal(str(m["daily_returned"]))
    cum_roi = ((cum_returned - cum_staked) / cum_staked * 100) if cum_staked > 0 else 0
    conn.execute("UPDATE daily_metrics SET cumulative_roi = ? WHERE date = ?",
                 (float(cum_roi), m["date"]))

conn.commit()

print(f"\n{'='*55}")
print(f"  SUMMARY FOR JULY 4-6")
print(f"{'='*55}")
print(f"  Total correct: {total_correct}/{len(rows)} ({total_correct/len(rows)*100:.1f}%)")
print(f"  Total staked: ${float(total_staked):.2f}")
print(f"  Total returned: ${float(total_returned):.2f}")
print(f"  Net: ${float(total_returned - total_staked):+.2f}")
print(f"{'='*55}")

conn.close()
print("\n✅ Complete!")
