"""
scripts/catchup_all_v2.py

Teljes catch-up:
1. Friss predikciok az upcoming R32 meccsekre real odds-okkal
2. ELO frissites a csoportkori meccsek alapjan
3. Round of 16 bracket elokeszitese

Hasznalat:
  python scripts/catchup_all_v2.py
"""
import sys, os
from datetime import date, timedelta
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import requests
import numpy as np
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection
from modell.train import load_model, predict_proba
from modell.pipeline import _build_elo_timeline, _build_form_cache, build_prediction_row
from scripts.daily_predictor import _predict_match

# -- Konfig --
ELO_K_FACTOR = 40
STAKE = Decimal('10.00')
API_KEY_ODDS = os.environ.get("API_KEY_ODDS_API", "")
ODDS_SPORT = "soccer_fifa_world_cup"
ODDS_NAME_MAP = {
    "United States": "USA",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
    "Korea Republic": "South Korea",
    "IR Iran": "Iran",
}

def fetch_real_odds():
    """Fetch real odds from the Odds API."""
    if not API_KEY_ODDS:
        print("  Odds API kulcs nincs beallitva.")
        return []
    try:
        resp = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{ODDS_SPORT}/odds/",
            params={"apiKey": API_KEY_ODDS, "regions": "eu,us,uk", "markets": "h2h", "oddsFormat": "decimal"},
            timeout=10
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"  Odds API hiba: {resp.status_code} - {resp.text[:100]}")
            return []
    except Exception as e:
        print(f"  Odds API kivetel: {e}")
        return []

def get_match_odds(odds_data, home_name, away_name):
    """Get odds for a specific matchup."""
    def _norm(n):
        rev_map = {v: k for k, v in ODDS_NAME_MAP.items()}
        return rev_map.get(n, n)

    h_api = _norm(home_name)
    a_api = _norm(away_name)
    defaults = {"home": 2.0, "draw": 3.1, "away": 2.0}

    match = next((m for m in odds_data if m.get("home_team") == h_api and m.get("away_team") == a_api), None)
    # Also try reversed
    if not match:
        match = next((m for m in odds_data if m.get("home_team") == a_api and m.get("away_team") == h_api), None)
    if not match:
        return defaults, None

    bms = match.get("bookmakers", [])
    bm = bms[0] if bms else None
    if not bm:
        return defaults, None

    market = next((mk for mk in bm.get("markets", []) if mk["key"] == "h2h"), None)
    if not market:
        return defaults, None

    result = dict(defaults)
    for outcome in market.get("outcomes", []):
        nm = outcome.get("name", "")
        px = float(outcome.get("price", 2.0))
        if nm == match["home_team"]:
            result["home"] = px
        elif nm == match["away_team"]:
            result["away"] = px
        elif nm.lower() == "draw":
            result["draw"] = px

    return result, match.get("id")


def step1_generate_upcoming_predictions(conn, odds_data):
    """Generate predictions for upcoming R32 matches with real odds."""
    print("\n--- 1. Lepes: R32 predikciok real odds-okkal ---")

    model, feature_names = load_model()
    print(f"  Model betoltve: {type(model).__name__}")
    elo_tl = _build_elo_timeline(conn)
    form_cache = _build_form_cache(conn)
    print(f"  ELO timeline: {len(elo_tl)}, Form cache: {len(form_cache)}")
    print(f"  Odds API-tol: {len(odds_data)} meccs szorzoi")

    # Upcoming R32 matches (not yet played)
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

    print(f"  Meg nem kezdodott R32 meccsek: {len(upcoming)}")

    inserted = 0
    for m in upcoming:
        home_id = m["home_team_id"]
        away_id = m["away_team_id"]
        home_name = m["home_name"]
        away_name = m["away_name"]
        match_date = m["match_date"]
        stage = m["stage"]

        # Check if prediction already exists for this match
        existing = conn.execute("""
            SELECT id FROM daily_predictions
            WHERE match_date=? AND home_team_id=? AND away_team_id=?
        """, (match_date, home_id, away_id)).fetchone()

        if existing:
            print(f"     [MEGVAN] {home_name} vs {away_name}")
            continue

        # Run model prediction
        probs = _predict_match(conn, model, feature_names, elo_tl, form_cache, home_id, away_id, stage)
        away_p, draw_p, home_p = probs

        best_idx = int(np.argmax(probs))
        if best_idx == 2:
            pred_winner_id = home_id
            target_odds_key = "home"
            prob_val = Decimal(str(home_p))
            pred_label = home_name
        elif best_idx == 0:
            pred_winner_id = away_id
            target_odds_key = "away"
            prob_val = Decimal(str(away_p))
            pred_label = away_name
        else:
            pred_winner_id = None
            target_odds_key = "draw"
            prob_val = Decimal(str(draw_p))
            pred_label = "Dontetlen"

        # Get real odds
        odds, _ = get_match_odds(odds_data, home_name, away_name)
        target_odds = Decimal(str(odds[target_odds_key]))

        # EV calculation
        ev = (STAKE * target_odds * prob_val) - STAKE

        ev_tag = "VALUE BET" if ev > 0 else "negativ EV"

        # Save
        conn.execute("""
            INSERT INTO daily_predictions
                (match_date, home_team_id, away_team_id, predicted_winner_id,
                 odds_home, odds_draw, odds_away, target_odds, stake, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """, (match_date, home_id, away_id, pred_winner_id,
              odds["home"], odds["draw"], odds["away"],
              float(target_odds), float(STAKE)))

        print(f"     [UJ] {match_date}: {home_name:25s} vs {away_name:25s}")
        print(f"          Tipp: {pred_label:25s} | Odds: {target_odds:.2f} | EV: {ev:+.2f} ({ev_tag})")
        print(f"          Probs: H:{home_p:.1%} D:{draw_p:.1%} V:{away_p:.1%}")
        inserted += 1

    conn.commit()
    print(f"  Osszesen: {inserted} uj predikcio letrehozva.\n")


def step2_update_elo(conn):
    """Update static_elo table based on all 2026 group stage results."""
    print("\n--- 2. Lepes: ELO frissites ---")

    # Get all group stage matches with scores, ordered chronologically
    matches = conn.execute("""
        SELECT m.id, m.match_date, m.home_team_id, m.away_team_id,
               m.home_score, m.away_score, t1.name as home, t2.name as away
        FROM match m
        JOIN team t1 ON m.home_team_id = t1.id
        JOIN team t2 ON m.away_team_id = t2.id
        JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage LIKE 'Group%' AND m.home_score IS NOT NULL
        ORDER BY m.match_date, m.id
    """).fetchall()

    print(f"  Csoportkori meccsek: {len(matches)}")

    updated = 0
    for m in matches:
        home_id = m["home_team_id"]
        away_id = m["away_team_id"]
        home_goals = m["home_score"]
        away_goals = m["away_score"]

        h_elo_row = conn.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (home_id,)).fetchone()
        a_elo_row = conn.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (away_id,)).fetchone()

        if not h_elo_row or not a_elo_row:
            continue

        r_h = h_elo_row["elo_rating"]
        r_a = a_elo_row["elo_rating"]

        # Expected score
        e_h = 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))
        e_a = 1.0 - e_h

        # Actual score
        if home_goals > away_goals:
            s_h, s_a = 1.0, 0.0
        elif home_goals == away_goals:
            s_h, s_a = 0.5, 0.5
        else:
            s_h, s_a = 0.0, 1.0

        new_r_h = r_h + ELO_K_FACTOR * (s_h - e_h)
        new_r_a = r_a + ELO_K_FACTOR * (s_a - e_a)

        conn.execute("UPDATE static_elo SET elo_rating=? WHERE team_id=?", (new_r_h, home_id))
        conn.execute("UPDATE static_elo SET elo_rating=? WHERE team_id=?", (new_r_a, away_id))
        updated += 1

    conn.commit()
    print(f"  {updated} meccs ELO-ja frissitve.\n")


def step3_prepare_r16(conn):
    """Prepare Round of 16 matchups based on R32 results."""
    print("\n--- 3. Lepes: Round of 16 bracket ---")

    # Check if tournament 25 already has Round of 16 matches
    existing_r16 = conn.execute("""
        SELECT COUNT(*) FROM match m
        JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage = 'Round of 16'
    """).fetchone()[0]

    if existing_r16 > 0:
        print(f"  Mar van {existing_r16} Round of 16 meccs az adatbazisban.")
        return

    # The Round of 16 matchups after Round of 32 are determined by the bracket.
    # 2026 format: 32 teams -> 16 advance.
    # Let's wait until R32 results are known before creating R16 matches.
    # For now, just log the situation.
    r32_played = conn.execute("""
        SELECT COUNT(*) FROM match m
        JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage = 'Round of 32' AND m.home_score IS NOT NULL
    """).fetchone()[0]

    r32_total = conn.execute("""
        SELECT COUNT(*) FROM match m
        JOIN tournament t ON m.tournament_id = t.id
        WHERE t.year = 2026 AND m.stage = 'Round of 32'
    """).fetchone()[0]

    print(f"  Round of 32: {r32_played}/{r32_total} meccs lejatszva.")
    if r32_played < r32_total:
        print(f"  Meg varunk a Round of 16 letrehozasaval, amig nincs meg az osszes R32 eredmeny.")
    else:
        print(f"  Minden R32 meccs kesz! Adju hozza az R16 meccseket...")
        # TODO: Implement R16 bracket generation


def main():
    print("=" * 58)
    print("  Catch-up v2: predikciok + odds + ELO")
    print("=" * 58)

    conn = get_connection()

    try:
        # Fetch real odds first
        print(f"\n  Odds API lekerdezese...")
        odds_data = fetch_real_odds()
        print(f"  {len(odds_data)} meccshez van odds adat.")

        step1_generate_upcoming_predictions(conn, odds_data)

        step2_update_elo(conn)

        step3_prepare_r16(conn)

        print(f"\n{'='*58}")
        print(f"  Catch-up v2 kesz!")
        print(f"{'='*58}")

    except Exception as e:
        conn.rollback()
        print(f"\nHIBA: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
