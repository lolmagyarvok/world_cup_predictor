"""
scripts/catchup_predictions.py

Catch-up script for missed daily predictions (June 21-29).
Generates predictions for past matches using the model (retroactively)
and immediately evaluates them since we know the actual results.
"""
import sys
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection
from modell.pipeline import build_prediction_row, _build_elo_timeline, _build_form_cache
from modell.train import load_model, predict_proba

STAKE = Decimal('10.00')


def generate_and_save_predictions(conn, cursor, model, feature_names, elo_tl, form_cache, target_date):
    """Generate predictions for a past date's matches and save + evaluate them."""
    # Find all matches on this date that have results but no daily_predictions entry
    matches = cursor.execute("""
        SELECT m.id, m.home_team_id, m.away_team_id, m.stage,
               m.home_score, m.away_score,
               h.name AS home_name, a.name AS away_name
        FROM match m
        JOIN team h ON h.id = m.home_team_id
        JOIN team a ON a.id = m.away_team_id
        JOIN tournament t ON t.id = m.tournament_id
        WHERE t.year = 2026
          AND m.match_date = ?
          AND m.home_score IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM daily_predictions dp
              WHERE dp.match_date = ?
                AND dp.home_team_id = m.home_team_id
                AND dp.away_team_id = m.away_team_id
          )
        ORDER BY m.match_date
    """, (target_date, target_date)).fetchall()

    if not matches:
        return 0, 0

    total = 0
    correct = 0

    for m in matches:
        home_id = m["home_team_id"]
        away_id = m["away_team_id"]
        home_name = m["home_name"]
        away_name = m["away_name"]
        stage = m["stage"] or "Group stage"
        h_score = m["home_score"]
        a_score = m["away_score"]

        # Determine actual winner
        if h_score > a_score:
            actual_winner_id = home_id
        elif a_score > h_score:
            actual_winner_id = away_id
        else:
            actual_winner_id = None

        # Run model prediction
        try:
            X = build_prediction_row(conn, home_id, away_id, stage, elo_tl, form_cache)
            raw = predict_proba(model, feature_names, X)[0]
            classes = list(model.classes_)
            idx_away = classes.index(0)
            idx_draw = classes.index(1)
            idx_home = classes.index(2)
            probs = np.array([raw[idx_away], raw[idx_draw], raw[idx_home]])
        except Exception as e:
            print(f"    Model error ({home_name} vs {away_name}): {e}")
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
            target_odds = Decimal('2.00')
            prob_val = Decimal(str(home_p))
            pred_label = home_name
        elif best_idx == 0:
            pred_winner_id = away_id
            target_odds = Decimal('2.00')
            prob_val = Decimal(str(away_p))
            pred_label = away_name
        else:
            pred_winner_id = None
            target_odds = Decimal('3.10')
            prob_val = Decimal(str(draw_p))
            pred_label = "Dontetlen"

        # Determine if prediction was correct
        is_correct = (pred_winner_id is None and actual_winner_id is None) or \
                     (pred_winner_id is not None and pred_winner_id == actual_winner_id)
        if is_correct:
            correct += 1

        # Save to daily_predictions with EVALUATED status
        conn.execute("""
            INSERT INTO daily_predictions
                (match_date, home_team_id, away_team_id,
                 predicted_winner_id,
                 odds_home, odds_draw, odds_away,
                 target_odds, stake, status, actual_winner_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'EVALUATED', ?)
        """, (
            target_date, home_id, away_id,
            pred_winner_id,
            2.0, 3.1, 2.0,  # default odds
            float(target_odds), float(STAKE),
            actual_winner_id
        ))
        total += 1

        sc = "CORRECT" if is_correct else "WRONG"
        actual_label = actual_winner_id if actual_winner_id else "Dontetlen"
        actual_name = next(
            (r["name"] for r in
             cursor.execute("SELECT name FROM team WHERE id=?", (actual_winner_id,)).fetchall()
             if actual_winner_id is not None),
            "Dontetlen"
        ) if actual_winner_id is not None else "Dontetlen"
        print(f"    {sc:7s} | {home_name:25s} {h_score}-{a_score} {away_name:25s} | Pred: {pred_label:25s} | Actual: {actual_name:25s}")

    return total, correct


def update_metrics(conn, cursor, eval_date, total, correct):
    """Update daily_metrics for this date."""
    staked = Decimal(str(total)) * STAKE
    returned = Decimal('0.00')
    # We can't know the exact odds that would have been available,
    # so we use the default odds and calculate a simplified return.
    # For actual odds-based returns, the evaluator handles it.

    prev = cursor.execute("""
        SELECT SUM(daily_staked) AS total_s, SUM(daily_returned) AS total_r
        FROM daily_metrics WHERE date < ?
    """, (eval_date,)).fetchone()
    prev_staked = Decimal(str(prev["total_s"] or '0'))
    prev_returned = Decimal(str(prev["total_r"] or '0'))
    cum_staked = prev_staked + staked
    cum_returned = prev_returned + returned
    cum_roi = ((cum_returned - cum_staked) / cum_staked * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if cum_staked > 0 else Decimal('0.00')
    accuracy = (correct / total * 100) if total > 0 else 0.0

    cursor.execute("""
        INSERT INTO daily_metrics
            (date, matches_evaluated, correct_predictions,
             daily_staked, daily_returned, cumulative_roi, accuracy)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            matches_evaluated = excluded.matches_evaluated,
            correct_predictions = excluded.correct_predictions,
            daily_staked = excluded.daily_staked,
            daily_returned = excluded.daily_returned,
            cumulative_roi = excluded.cumulative_roi,
            accuracy = excluded.accuracy
    """, (eval_date, total, correct, float(staked), float(returned), float(cum_roi), accuracy))

    return cum_roi, accuracy, staked


def main():
    print("=" * 60)
    print("  Catch-up: Missed Predictions (June 21-29)")
    print("=" * 60)

    # Dates that were missed
    missed_dates = []
    d = date(2026, 6, 21)
    end = date(2026, 6, 29)
    while d <= end:
        missed_dates.append(str(d))
        d += timedelta(days=1)

    conn = get_connection()
    try:
        cursor = conn.cursor()

        print("\n  Loading model...")
        model, feature_names = load_model()
        print(f"  Model loaded ({type(model).__name__}), {len(feature_names)} features")

        print("\n  Building ELO timeline and form cache...")
        elo_tl = _build_elo_timeline(conn)
        form_cache = _build_form_cache(conn)
        print(f"  ELO snapshots: {len(elo_tl)}, Form entries: {len(form_cache)}")

        total_preds = 0
        total_correct = 0
        total_days = 0

        for target_date in missed_dates:
            print(f"\n  --- {target_date} ---")
            t, c = generate_and_save_predictions(
                conn, cursor, model, feature_names, elo_tl, form_cache, target_date
            )
            if t > 0:
                cum_roi, acc, staked_amt = update_metrics(conn, cursor, target_date, t, c)
                total_preds += t
                total_correct += c
                total_days += 1
                acc_pct = (c / t * 100) if t > 0 else 0
                print(f"  => {t} matches, {c} correct ({acc_pct:.1f}%)")
            else:
                print(f"  => No missing matches found for this date")

        conn.commit()

        print(f"\n{'=' * 60}")
        if total_days > 0:
            overall_acc = (total_correct / total_preds * 100) if total_preds > 0 else 0
            print(f"  Catch-up complete: {total_preds} predictions across {total_days} days")
            print(f"  Overall accuracy: {total_correct}/{total_preds} = {overall_acc:.1f}%")
        else:
            print(f"  No missing predictions found (already generated or no matches).")
        print(f"{'=' * 60}\n")

    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
