"""
scripts/daily_predictor.py

Napi futtatású szkript (pl. reggel 8:10, az evaluator_daily.py után).
"""

import argparse
import sys
from datetime import date
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

import requests
import numpy as np
from dotenv import load_dotenv
import os

# Színek
try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
    G, R, Y, B, W, DIM, RESET = Fore.GREEN, Fore.RED, Fore.YELLOW, Fore.CYAN, Fore.WHITE, Style.DIM, Style.RESET_ALL
except ImportError:
    G = R = Y = B = W = DIM = RESET = ""

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection
from modell.pipeline import build_prediction_row, _build_elo_timeline, _build_form_cache
from modell.train import load_model, predict_proba

# ── Konfiguráció ──────────────────────────────────────────────────────────────

API_KEY_ODDS = os.environ.get("API_KEY_ODDS_API", "")
ODDS_SPORT   = "soccer_fifa_world_cup"
ODDS_REGIONS = "eu"
ODDS_MARKETS = "h2h"
ODDS_FORMAT  = "decimal"
STAKE        = Decimal('10.00')  # DECIMAL! Ezt megjegyezni!

ODDS_NAME_MAP = {
    "United States": "USA",
    "Korea Republic": "South Korea",
    "IR Iran": "Iran",
}


def fetch_odds() -> list[dict]:
    if not API_KEY_ODDS:
        print("  ⚠️  API_KEY_ODDS_API nincs beállítva – alapértelmezett szorzókkal dolgozunk.")
        return []

    url = f"https://api.the-odds-api.com/v4/sports/{ODDS_SPORT}/odds/"
    params = {
        "apiKey":      API_KEY_ODDS,
        "regions":     ODDS_REGIONS,
        "markets":     ODDS_MARKETS,
        "oddsFormat":  ODDS_FORMAT,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  Odds API hiba: {e}")
        return []


def _get_match_odds(odds_data: list[dict], home_name: str, away_name: str,
                    bookmaker_pref: str = "Betclic") -> dict:
    def _norm(n):
        rev_map = {v: k for k, v in ODDS_NAME_MAP.items()}
        return rev_map.get(n, n)

    h_api = _norm(home_name)
    a_api = _norm(away_name)

    match = next((m for m in odds_data if m.get("home_team") == h_api and m.get("away_team") == a_api), None)
    
    # Decimal alapú defaultok
    defaults = {"home": Decimal('2.00'), "draw": Decimal('3.10'), "away": Decimal('2.00')}
    if not match:
        return defaults

    bms = match.get("bookmakers", [])
    bm  = next((b for b in bms if b["key"].lower() == bookmaker_pref.lower()), None)
    if not bm and bms:
        bm = bms[0]
    if not bm:
        return defaults

    market = next((mk for mk in bm.get("markets", []) if mk["key"] == "h2h"), None)
    if not market:
        return defaults

    result = dict(defaults)
    for outcome in market.get("outcomes", []):
        nm = outcome.get("name", "")
        # Konvertáljuk Decimal-ra azonnal
        px = Decimal(str(outcome.get("price", 2.0)))
        
        if nm == match["home_team"]:
            result["home"] = px
        elif nm == match["away_team"]:
            result["away"] = px
        elif nm.lower() == "draw":
            result["draw"] = px

    return result


def _predict_match(conn, model, feature_names, elo_tl, form_cache,
                   home_id: int, away_id: int, stage: str) -> np.ndarray:
    try:
        X = build_prediction_row(conn, home_id, away_id, stage, elo_tl, form_cache)
        raw = predict_proba(model, feature_names, X)[0]

        classes   = list(model.classes_)
        idx_away  = classes.index(0)
        idx_draw  = classes.index(1)
        idx_home  = classes.index(2)

        return np.array([raw[idx_away], raw[idx_draw], raw[idx_home]])

    except Exception as e:
        print(f"    ⚠️  Modell hiba ({home_id} vs {away_id}): {e} – ELO fallback")
        h_row = conn.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (home_id,)).fetchone()
        a_row = conn.execute("SELECT elo_rating FROM static_elo WHERE team_id=?", (away_id,)).fetchone()
        r_h   = h_row["elo_rating"] if h_row else 1500.0
        r_a   = a_row["elo_rating"] if a_row else 1500.0
        p_h   = 1.0 / (1.0 + 10 ** ((r_a - r_h) / 400.0))
        p_d   = 0.22
        p_a   = max(0.01, 1 - p_h - p_d)
        arr   = np.array([p_a, p_d, max(0.01, p_h - p_d / 2)])
        return arr / arr.sum()


def run(pred_date: str | None = None) -> None:
    if pred_date is None:
        pred_date = str(date.today())

    print(f"\n{'='*58}")
    print(f"  [daily_predictor.py]  Predikciók dátuma: {pred_date}")
    print(f"{'='*58}")

    conn = get_connection()
    
    # ── Fő Tranzakciós Blokk ──
    try:
        tbl = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_predictions'").fetchone()
        if not tbl:
            print("\n  ❌ 'daily_predictions' tábla hiányzik. Futtasd előbb az update_results.py-t!")
            return

        today_matches = conn.execute("""
            SELECT m.id, m.home_team_id, m.away_team_id, m.stage,
                   h.name AS home_name, a.name AS away_name
            FROM match m
            JOIN team h ON h.id = m.home_team_id
            JOIN team a ON a.id = m.away_team_id
            JOIN tournament t ON t.id = m.tournament_id
            WHERE t.year = 2026
              AND m.match_date = ?
              AND m.home_score IS NULL
            ORDER BY m.match_date
        """, (pred_date,)).fetchall()

        if not today_matches:
            print(f"\n  ℹ️  Nincs mai meccs az adatbázisban ({pred_date}).")
            return

        print(f"\n  {len(today_matches)} mai meccs találva.\n")

        print("  Modell betöltése...")
        model, feature_names = load_model()

        elo_tl     = _build_elo_timeline(conn)
        form_cache = _build_form_cache(conn)

        print("  Szorzók lekérése...")
        odds_data = fetch_odds()
        
        inserted = 0
        print(f"  {'─'*56}")
        print(f"  {B}  🔮  MAI PREDIKCIÓK{RESET}")
        print(f"  {'─'*56}")

        for m in today_matches:
            home_id   = m["home_team_id"]
            away_id   = m["away_team_id"]
            home_name = m["home_name"]
            away_name = m["away_name"]
            stage     = m["stage"] or "Group stage"

            probs = _predict_match(conn, model, feature_names, elo_tl, form_cache, home_id, away_id, stage)
            away_p, draw_p, home_p = probs

            best_idx = int(np.argmax(probs))
            if best_idx == 2:
                pred_winner_id = home_id
                target_odds_key = "home"
                pred_label = f"{G}{home_name}{RESET}"
                prob_val = Decimal(str(home_p))
            elif best_idx == 0:
                pred_winner_id = away_id
                target_odds_key = "away"
                pred_label = f"{Y}{away_name}{RESET}"
                prob_val = Decimal(str(away_p))
            else:
                pred_winner_id = None  # NULL az adatbázisban = döntetlen
                target_odds_key = "draw"
                pred_label = f"{DIM}Döntetlen{RESET}"
                prob_val = Decimal(str(draw_p))

            odds = _get_match_odds(odds_data, home_name, away_name)
            target_odds = odds[target_odds_key]

            # EV Számítás biztonságosan (Decimal)
            ev = (STAKE * target_odds * prob_val) - STAKE
            ev_color = G if ev > 0 else R

            # VALUE BET LOGIKA: Ha nagyon negatív az EV, lehet, hogy nem is kéne fogadni, 
            # de egyelőre lementjük a szimuláció kedvéért, csak jelezzük.
            if ev < 0:
                ev_warning = f"{R}(Veszteséges stratégia!){RESET}"
            else:
                ev_warning = f"{G}(Value Bet!){RESET}"

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
                    float(odds["home"]), float(odds["draw"]), float(odds["away"]), # SQLite floatot vár itt
                    float(target_odds), float(STAKE)
                ))
                inserted += 1
                tag = f"{G}[ÚJ]{RESET}"
            else:
                tag = f"{DIM}[már megvan]{RESET}"

            print(f"  {tag} {home_name} vs {away_name}")
            print(f"    Szakasz: {stage}")
            print(f"    Valószínűségek → Hazai: {home_p:.1%}  |  Döntetlen: {draw_p:.1%}  |  Vendég: {away_p:.1%}")
            print(f"    Tipp: {pred_label}  |  Szorzó: {W}{target_odds:.2f}{RESET}  |  Tét: ${STAKE}")
            print(f"    Várható érték (EV): {ev_color}${ev:.2f}{RESET} {ev_warning}\n")

        # MINDEN SIKERES? Akkor commit!
        conn.commit()
        
        print(f"  {'═'*56}")
        print(f"  {W}  {inserted} új predikció mentve a daily_predictions táblába.{RESET}")
        print(f"  {'═'*56}\n")
        print("  ✅ daily_predictor.py kész.\n")

    except Exception as e:
        conn.rollback()
        print(f"\n  ❌ KRITIKUS HIBA A PREDIKCIÓ SORÁN: {e}")
        print("  Rollback megtörtént, az adatbázis nem sérült.")
    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VB napi prediktor")
    parser.add_argument("--date", type=str, default=None, help="Dátum YYYY-MM-DD formátumban (alapért.: ma)")
    args = parser.parse_args()
    run(args.date)