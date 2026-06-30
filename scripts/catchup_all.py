"""
catchup_all.py

Osszevont catch-up script, ami elvegez minden kimaradt lepest:
1. Predikciok szinkronizalasa a valos eredmenyekkel
2. Kiértékelés (evaluator) az osszes READY_FOR_EVAL predikciora
3. Uj predikciok generalasa a mai (es megnem-tortent) R32 meccsekre
4. ELO frissites (statikus) a csoportkori eredmenyek alapjan

Hasznalat:
  python scripts/catchup_all.py
"""

import sys, os
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection

# Konstans
ELO_K_FACTOR = 40

def step1_sync_predictions(conn):
    """Minden PENDING predikciot szinkronizal a match tabla eredmenyeivel."""
    cur = conn.cursor()
    pending = cur.execute("""
        SELECT dp.id, dp.match_date, dp.home_team_id, dp.away_team_id,
               m.home_score, m.away_score, t1.name as home, t2.name as away
        FROM daily_predictions dp
        JOIN match m ON m.home_team_id = dp.home_team_id
                    AND m.away_team_id = dp.away_team_id
        JOIN tournament t ON m.tournament_id = t.id
        JOIN team t1 ON dp.home_team_id = t1.id
        JOIN team t2 ON dp.away_team_id = t2.id
        WHERE dp.status = 'PENDING' AND t.year = 2026 AND m.home_score IS NOT NULL
        ORDER BY dp.match_date
    """).fetchall()

    synced = 0
    dates = set()
    for row in pending:
        h, a = row["home_score"], row["away_score"]
        winner_id = row["home_team_id"] if h > a else (row["away_team_id"] if a > h else None)
        cur.execute("UPDATE daily_predictions SET actual_winner_id=?, status='READY_FOR_EVAL' WHERE id=?",
                   (winner_id, row["id"]))
        dates.add(row["match_date"])
        synced += 1

    conn.commit()
    print(f"  1. Szinkronizalva: {synced} predikcio")
    return sorted(dates)


def step2_evaluate(conn, dates):
    """Kiertekel minden READY_FOR_EVAL predikciot datumonkent."""
    if not dates:
        print("  2. Nincs kiertekelendo predikcio.")
        return

    from scripts.evaluator_daily import evaluate_day, update_daily_metrics, print_report
    from decimal import Decimal

    total_eval = 0
    for d in dates:
        cur = conn.cursor()
        stats = evaluate_day(cur, d)
        if stats:
            cum_roi = update_daily_metrics(cur, stats)
            conn.commit()
            total_eval += stats["total"]
            print(f"  2. {d}: {stats['total']} meccs, {stats['correct']} helyes ({stats['accuracy']:.1f}%), ROI: {stats['daily_roi']:+.2f}%")
        else:
            print(f"  2. {d}: nincs READY_FOR_EVAL adat")

    print(f"     Osszesen: {total_eval} predikcio kiertekelese kesz")


def step3_generate_r32_predictions(conn):
    """General predikciokat a meg nem kezdodott R32 meccsekre (ma es jovobeli)."""
    from scripts.daily_predictor import _predict_match, _get_match_odds, STAKE

    print("\n  3. R32 predikciok generalasa...")

    # Modell betoltese
    from modell.train import load_model, predict_proba
    from modell.pipeline import _build_elo_timeline, _build_form_cache

    model, feature_names = load_model()
    elo_tl = _build_elo_timeline(conn)
    form_cache = _build_form_cache(conn)

    # R32 meccsek, amik meg nem kezdodtek el (home_score IS NULL)
    upcoming = conn.execute("""
        SELECT m.id, m.match_date, m.home_team_id, m.away_team_id, m.stage,
               t1.name as home_name, t2.name as away_name
        FROM match m
        JOIN team t1 ON m.home_team_id = t1.id
        JOIN team t2 ON m.away_team_id = t2.id
        JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage = 'Round of 32' AND m.home_score IS NULL
        ORDER BY m.match_date
    """).fetchall()

    if not upcoming:
        print("     Nincs meg nem kezdodott R32 meccs.")
        return

    inserted = 0
    for m in upcoming:
        home_id = m["home_team_id"]
        away_id = m["away_team_id"]
        home_name = m["home_name"]
        away_name = m["away_name"]
        match_date = m["match_date"]
        stage = m["stage"]

        # Van-e mar predikcio?
        existing = conn.execute("""
            SELECT id FROM daily_predictions
            WHERE match_date=? AND home_team_id=? AND away_team_id=?
        """, (match_date, home_id, away_id)).fetchone()
        if existing:
            print(f"     {home_name} vs {away_name} - mar van predikcio (id={existing['id']})")
            continue

        # Predikcio
        probs = _predict_match(conn, model, feature_names, elo_tl, form_cache, home_id, away_id, stage)
        away_p, draw_p, home_p = probs

        best_idx = int(probs.argmax())
        if best_idx == 2:
            pred_winner_id = home_id
            target_odds_key = "home"
            prob_val = home_p
        elif best_idx == 0:
            pred_winner_id = away_id
            target_odds_key = "away"
            prob_val = away_p
        else:
            pred_winner_id = None
            target_odds_key = "draw"
            prob_val = draw_p

        # Odds (defaultok - az odds API-t nem hivjuk, mert nincs kulcs)
        odds = {"home": 2.0, "draw": 3.1, "away": 2.0}
        target_odds = odds[target_odds_key]

        conn.execute("""
            INSERT INTO daily_predictions
                (match_date, home_team_id, away_team_id, predicted_winner_id,
                 odds_home, odds_draw, odds_away, target_odds, stake, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """, (match_date, home_id, away_id, pred_winner_id,
              odds["home"], odds["draw"], odds["away"],
              target_odds, float(STAKE)))

        winner_name = home_name if pred_winner_id == home_id else (away_name if pred_winner_id == away_id else "Dontetlen")
        print(f"     [UJ] {match_date}: {home_name} vs {away_name} -> Tipp: {winner_name} (p={prob_val:.1%})")
        inserted += 1

    conn.commit()
    print(f"     {inserted} uj R32 predikcio letrehozva.")


def step4_update_elo(conn):
    """Frissiti a static_elo-t a csoportkori eredmenyek alapjan."""
    print("\n  4. ELO frissites...")

    cur = conn.cursor()

    # Csoportkori meccsek rendezve, hogy szimulaljuk a timeline-t
    matches = cur.execute("""
        SELECT m.id, m.match_date, m.home_team_id, m.away_team_id,
               m.home_score, m.away_score, t1.name as home, t2.name as away
        FROM match m
        JOIN team t1 ON m.home_team_id = t1.id
        JOIN team t2 ON m.away_team_id = t2.id
        JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage LIKE 'Group%' AND m.home_score IS NOT NULL
        ORDER BY m.match_date, m.id
    """).fetchall()

    if not matches:
        print("     Nincs csoportkori meccs ELO frissiteshez.")
        return

    updated = 0
    for m in matches:
        home_id = m["home_team_id"]
        away_id = m["away_team_id"]
        home_goals = m["home_score"]
        away_goals = m["away_score"]

        # Aktualis ELO-k
        h_elo_row = cur.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (home_id,)).fetchone()
        a_elo_row = cur.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (away_id,)).fetchone()

        if not h_elo_row or not a_elo_row:
            print(f"     Kihagyva (nincs ELO): {m['home']} vs {m['away']}")
            continue

        r_h = h_elo_row["elo_rating"]
        r_a = a_elo_row["elo_rating"]

        # Varhato eredmeny
        e_h = 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))
        e_a = 1.0 - e_h

        # Tenyleges eredmeny
        if home_goals > away_goals:
            s_h, s_a = 1.0, 0.0
        elif home_goals == away_goals:
            s_h, s_a = 0.5, 0.5
        else:
            s_h, s_a = 0.0, 1.0

        new_r_h = r_h + ELO_K_FACTOR * (s_h - e_h)
        new_r_a = r_a + ELO_K_FACTOR * (s_a - e_a)

        cur.execute("UPDATE static_elo SET elo_rating=? WHERE team_id=?", (new_r_h, home_id))
        cur.execute("UPDATE static_elo SET elo_rating=? WHERE team_id=?", (new_r_a, away_id))
        updated += 1

    conn.commit()
    print(f"     {updated} meccs ELO-ja frissitve.")


def main():
    print("=" * 58)
    print("  Catch-up: predikcio szinkronizacio, ertekeles, R32, ELO")
    print("=" * 58)

    conn = get_connection()

    print("\n--- 1. lepes: Predikciok szinkronizalasa ---")
    dates = step1_sync_predictions(conn)

    print("\n--- 2. lepes: Kiertekelés ---")
    step2_evaluate(conn, dates)

    # Ha nincs mentalist a listaba, adjuk hozza a mai napot is
    # (a meg nem kezdodott R32 meccsek)
    print("\n--- 3. lepes: R32 predikciok generalasa ---")
    step3_generate_r32_predictions(conn)

    print("\n--- 4. lepes: ELO frissites ---")
    step4_update_elo(conn)

    conn.close()
    print("\n" + "=" * 58)
    print("  Catch-up kesz!")
    print("=" * 58)


if __name__ == "__main__":
    main()
