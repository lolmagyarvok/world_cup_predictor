"""
sync_predictions.py

Szinkroniz�lja a daily_predictions-t a val�s meccseredm�nyekkel:
1. Minden PENDING predikci�hoz be�rja az actual_winner_id-t a match t�bla alapj�n
2. St�tuszt PENDING -> READY_FOR_EVAL-re �ll�tja
3. Majd megh�vja az evaluator_daily.py-t minden olyan napra, ahol van READY_FOR_EVAL

Haszn�lat:
  python scripts/sync_predictions.py
"""

import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection

def sync_all_pending_predictions(conn):
    """Minden PENDING predikciot szinkronizal a match tabla valos eredmenyeivel."""
    cur = conn.cursor()

    # PENDING predikciok amikhez van match eredmeny
    pending = cur.execute("""
        SELECT dp.id, dp.match_date, dp.home_team_id, dp.away_team_id,
               m.home_score, m.away_score,
               t1.name as home, t2.name as away
        FROM daily_predictions dp
        JOIN match m ON m.home_team_id = dp.home_team_id
                    AND m.away_team_id = dp.away_team_id
        JOIN tournament t ON m.tournament_id = t.id
        JOIN team t1 ON dp.home_team_id = t1.id
        JOIN team t2 ON dp.away_team_id = t2.id
        WHERE dp.status IN ('PENDING', 'EVALUATED')
          AND t.year = 2026
          AND m.home_score IS NOT NULL
        ORDER BY dp.match_date
    """).fetchall()

    print(f"\n  Szinkronizalhato predikciok: {len(pending)}")
    synced = 0
    dates_with_data = set()

    for row in pending:
        h, a = row["home_score"], row["away_score"]
        if h > a:
            winner_id = row["home_team_id"]
        elif a > h:
            winner_id = row["away_team_id"]
        else:
            winner_id = None

        cur.execute("""
            UPDATE daily_predictions
            SET actual_winner_id = ?, status = 'READY_FOR_EVAL'
            WHERE id = ?
        """, (winner_id, row["id"]))
        dates_with_data.add(row["match_date"])
        synced += 1

    conn.commit()

    if synced == 0:
        print("  Nincs szinkronizalando predikcio.")
    else:
        print(f"  {synced} predikcio szinkronizalva (-> READY_FOR_EVAL)")
        print(f"  Erintett datumok: {sorted(dates_with_data)}")

    return sorted(dates_with_data)


def main():
    print("=" * 55)
    print("  Predikciok szinkronizalasa a valos eredmenyekkel")
    print("=" * 55)

    conn = get_connection()
    dates = sync_all_pending_predictions(conn)
    conn.close()

    if dates:
        print(f"\n  Következő lépés: futtasd az evaluator_daily.py-t ezekre a dátumokra:")
        for d in dates:
            print(f"    python scripts/evaluator_daily.py --date {d}")
    else:
        print("\n  Nincs teendo.")

    print("\n  [+] sync_predictions.py kesz.")


if __name__ == "__main__":
    main()
