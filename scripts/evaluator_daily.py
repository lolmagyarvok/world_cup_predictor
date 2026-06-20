"""
scripts/evaluator_daily.py

Napi futtatású szkript (pl. reggel 8:05, az update_results.py után).
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

# Csomagoljuk try-except-be, ha a junior elfelejtette volna telepíteni a library-t
try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
    G = Fore.GREEN
    R = Fore.RED
    Y = Fore.YELLOW
    B = Fore.CYAN
    W = Fore.WHITE
    DIM = Style.DIM
    RESET = Style.RESET_ALL
except ImportError:
    print("⚠️ Figyelem: A 'colorama' csomag hiányzik. Színek nélkül indulunk. (pip install colorama)")
    G = R = Y = B = W = DIM = RESET = ""

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection


def evaluate_day(cursor, eval_date: str) -> dict:
    """
    Kiértékeli a 'READY_FOR_EVAL' státuszú predikciókat az adott dátumra.
    FIGYELEM: Nem commitol! A tranzakciót a main() véglegesíti.
    """
    
    # 1. SQL JOIN a memória-pazarlás és felesleges dictionary-k helyett
    cursor.execute("""
        SELECT dp.id, dp.stake, dp.target_odds,
               dp.predicted_winner_id, dp.actual_winner_id,
               th.name as home_name, ta.name as away_name,
               tp.name as pred_name, tact.name as actual_name
        FROM daily_predictions dp
        JOIN team th ON dp.home_team_id = th.id
        JOIN team ta ON dp.away_team_id = ta.id
        LEFT JOIN team tp ON dp.predicted_winner_id = tp.id
        LEFT JOIN team tact ON dp.actual_winner_id = tact.id
        WHERE dp.match_date = ? AND dp.status = 'READY_FOR_EVAL'
    """, (eval_date,))
    
    pending = cursor.fetchall()

    if not pending:
        return {}

    correct = 0
    staked = Decimal('0.00')
    returned = Decimal('0.00')
    match_details = []

    for row in pending:
        # 2. Decimal használata a pénzügyekhez a pontatlan float helyett
        stake = Decimal(str(row["stake"]))
        t_odds = Decimal(str(row["target_odds"]))
        
        pred_id = row["predicted_winner_id"]
        actual_id = row["actual_winner_id"]
        
        home_name = row["home_name"]
        away_name = row["away_name"]
        
        # NULL = döntetlen (egységesítve: daily_predictor, update_results)
        pred_str = row["pred_name"] if pred_id is not None else "Döntetlen"
        actual_str = row["actual_name"] if actual_id is not None else "Döntetlen"

        staked += stake
        
        # 3. Döntetlen kiértékelés: pred=None ∧ actual=None = találat (mindkettő döntetlen)
        #    Tipp nyert:  (a) döntetlent jósoltunk és döntetlen lett, VAGY
        #                  (b) ugyanazt a csapatot jósoltuk és nyert
        #    Fontos: actual_id=None akkor is, ha a meccs még nem történt meg — de
        #    az SQL WHERE status='READY_FOR_EVAL' garantálja, hogy actual_id már ki van töltve.
        is_correct = (pred_id is None and actual_id is None) or (pred_id is not None and pred_id == actual_id)

        if is_correct:
            correct += 1
            win = (stake * t_odds).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            returned += win
            result_str = f"{G}✅ NYERT  +${win}{RESET}"
        else:
            result_str = f"{R}❌ VESZÍT -${stake}{RESET}"

        match_details.append({
            "desc":   f"{home_name} vs {away_name}",
            "pred":   pred_str,
            "actual": actual_str,
            "odds":   float(t_odds),
            "result": result_str,
        })

        # Frissítjük a státuszt a kurzorral, de a commitot az adatbázis szintre bízzuk
        cursor.execute("UPDATE daily_predictions SET status='EVALUATED' WHERE id=?", (row["id"],))

    accuracy = (correct / len(pending)) * 100 if pending else 0.0
    net_profit = returned - staked
    daily_roi = (net_profit / staked * 100) if staked > Decimal('0') else Decimal('0.00')

    return {
        "date":        eval_date,
        "total":       len(pending),
        "correct":     correct,
        "staked":      staked,
        "returned":    returned,
        "net_profit":  net_profit,
        "daily_roi":   daily_roi,
        "accuracy":    accuracy,
        "details":     match_details,
    }


def update_daily_metrics(cursor, stats: dict) -> Decimal:
    """
    Frissíti a daily_metrics táblát.
    FIGYELEM: Ez a függvény sem commitol!
    """
    cursor.execute("""
        SELECT SUM(daily_staked) AS total_s, SUM(daily_returned) AS total_r
        FROM daily_metrics
        WHERE date < ?
    """, (stats["date"],))
    totals = cursor.fetchone()

    # Float/NULL értékek biztonságos átkonvertálása Decimal-ba
    prev_staked = Decimal(str(totals["total_s"] or '0'))
    prev_returned = Decimal(str(totals["total_r"] or '0'))
    
    cum_staked = prev_staked + stats["staked"]
    cum_returned = prev_returned + stats["returned"]
    
    if cum_staked > Decimal('0'):
        cum_roi = ((cum_returned - cum_staked) / cum_staked * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        cum_roi = Decimal('0.00')

    cursor.execute("""
        INSERT INTO daily_metrics
            (date, matches_evaluated, correct_predictions,
             daily_staked, daily_returned, cumulative_roi, accuracy)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            matches_evaluated   = excluded.matches_evaluated,
            correct_predictions = excluded.correct_predictions,
            daily_staked        = excluded.daily_staked,
            daily_returned      = excluded.daily_returned,
            cumulative_roi      = excluded.cumulative_roi,
            accuracy            = excluded.accuracy
    """, (
        stats["date"],
        stats["total"],
        stats["correct"],
        float(stats["staked"]),      # Az SQLite-ba visszamehet floatként, mivel ott REAL típus
        float(stats["returned"]),
        float(cum_roi),
        stats["accuracy"]
    ))
    
    return cum_roi


def print_report(stats: dict, cum_roi: Decimal) -> None:
    print(f"\n  {'─'*56}")
    print(f"  {B}  📊  NAPI TELJESÍTMÉNY – {stats['date']}{RESET}")
    print(f"  {'─'*56}")

    for d in stats["details"]:
        print(f"  {d['result']}")
        print(f"    {DIM}{d['desc']}{RESET}")
        print(f"    Tipp: {W}{d['pred']}{RESET}  |  Valós: {W}{d['actual']}{RESET}  |  Odds: {d['odds']:.2f}\n")

    profit_col = G if stats["net_profit"] >= Decimal('0') else R
    roi_col    = G if stats["daily_roi"] >= Decimal('0') else R
    cum_col    = G if cum_roi >= Decimal('0') else R

    print(f"  {'═'*56}")
    print(f"  {W}  ÖSSZESÍTŐ{RESET}")
    print(f"  {'═'*56}")
    print(f"  {'Kiértékelt meccsek':<28}  {stats['total']}")
    print(f"  {'Helyes tippek':<28}  {stats['correct']} / {stats['total']}  ({stats['accuracy']:.1f}%)")
    print(f"  {'Feltett tét (nap)':<28}  ${stats['staked']:.2f}")
    print(f"  {'Visszajött összeg':<28}  ${stats['returned']:.2f}")
    print(f"  {'Napi nyereség/veszteség':<28}  {profit_col}${stats['net_profit']:+.2f}{RESET}")
    print(f"  {'Napi ROI':<28}  {roi_col}{stats['daily_roi']:+.2f}%{RESET}")
    print(f"  {'── Kumulált ROI (torna) ──':<28}  {cum_col}{cum_roi:+.2f}%{RESET}")
    print(f"  {'═'*56}\n")


def main(eval_date: str | None = None) -> None:
    if eval_date is None:
        eval_date = str(date.today() - timedelta(days=1))

    print(f"\n{'='*55}")
    print(f"  [evaluator_daily.py]  Kiértékelési dátum: {eval_date}")
    print(f"{'='*55}")

    conn = get_connection()
    cursor = conn.cursor()

    # 4. KÖZPONTI TRANZAKCIÓKEZELÉS (Így kell ezt csinálni!)
    try:
        tbl = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_predictions'").fetchone()
        if not tbl:
            print("\n  ❌ 'daily_predictions' tábla nem létezik. Futtasd először az update_results.py-t!")
            return

        # Csak memóriában végezzük a módosításokat
        stats = evaluate_day(cursor, eval_date)

        if not stats:
            print(f"  ℹ️  Nincs kiértékelendő (READY_FOR_EVAL) adat a {eval_date} napra.")
            return

        # Metrikák sorba állítása
        cum_roi = update_daily_metrics(cursor, stats)

        # HA ÉS CSAK HA idáig minden hiba nélkül lefutott, akkor mentjük az adatbázist!
        conn.commit()

        # Biztonságos kiírás, miután az adatbázis frissült
        print_report(stats, cum_roi)
        print(f"  ✅ evaluator_daily.py sikeresen lefutott és elmentette az eredményeket.\n")

    except Exception as e:
        # Ha bármilyen KeyError, TypeError, Database lock történik, azonnali visszavonás!
        conn.rollback()
        print(f"\n  ❌ KRITIKUS HIBA A FELDOLGOZÁS SORÁN: {e}")
        print("  Az adatbázis érintetlen maradt. Keresd meg a hibát a kódban, és futtasd újra!")
    
    finally:
        # A kapcsolatot mindenképpen be kell zárni
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VB napi kiértékelő")
    parser.add_argument("--date", type=str, default=None, help="Dátum YYYY-MM-DD formátumban (alapért.: tegnap)")
    args = parser.parse_args()
    main(args.date)