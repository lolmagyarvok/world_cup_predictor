"""
simulation/tournament.py

Monte Carlo torna szimuláció a 2026-os FIFA Világbajnokságra.

Lépések:
  1. Csoportkör: minden meccs szimulálva (W/D/L valószínűségek)
  2. Csoportállás: pontok, gólkülönbség alapján rangsorolás
  3. Kieséses szakasz: Round of 32 → R16 → QF → SF → Döntő
  4. Futtatás N=10_000-szer → valószínűség eloszlás csapatonként

Kimenet: dict[team_name, dict[stage, probability]]
"""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection
from pipeline import build_prediction_row, _build_elo_timeline, _build_form_cache
from train import load_model, predict_proba

N_SIMULATIONS = 10_000
RANDOM_SEED   = 42


class Team(NamedTuple):
    id:   int
    name: str


# ── 2026 torna struktúra ──────────────────────────────────────────────────────

def load_2026_groups(conn: sqlite3.Connection) -> dict[str, list[Team]]:
    """
    Visszaadja a csoportokat: {"Group A": [Team, Team, Team, Team], ...}
    """
    rows = conn.execute("""
        SELECT DISTINCT m.stage,
               h.id AS h_id, h.name AS h_name,
               a.id AS a_id, a.name AS a_name
        FROM match m
        JOIN team h ON h.id = m.home_team_id
        JOIN team a ON a.id = m.away_team_id
        JOIN tournament t ON t.id = m.tournament_id
        WHERE t.year = 2026
      AND m.stage LIKE 'Group %'
      AND m.stage != 'Group stage'
        ORDER BY m.stage
    """).fetchall()

    groups: dict[str, set] = defaultdict(set)
    for r in rows:
        groups[r["stage"]].add(Team(r["h_id"], r["h_name"]))
        groups[r["stage"]].add(Team(r["a_id"], r["a_name"]))

    return {k: sorted(list(groups[k]), key=lambda t: t.name) for k in sorted(groups.keys())}


def load_2026_group_matches(conn: sqlite3.Connection) -> list[dict]:
    """
    Visszaadja az összes 2026-os csoportkör meccset.
    """
    return conn.execute("""
        SELECT m.id, m.stage,
               h.id AS home_id, h.name AS home_name,
               a.id AS away_id, a.name AS away_name,
               m.home_score, m.away_score
        FROM match m
        JOIN team h ON h.id = m.home_team_id
        JOIN team a ON a.id = m.away_team_id
        JOIN tournament t ON t.id = m.tournament_id
        WHERE t.year = 2026
      AND m.stage LIKE 'Group %'
      AND m.stage != 'Group stage'
        ORDER BY m.stage, m.match_date
    """).fetchall()


# ── Gólszám szimuláció (Poisson) ─────────────────────────────────────────────

def _expected_goals(elo_diff: float) -> tuple[float, float]:
    """
    ELO különbségből várható gólszámot becsül Poisson paraméternek.
    Kalibrálva a 2002-2022-es VB átlagokra (~2.5 gól/meccs).

    Minél nagyobb az ELO különbség, annál nagyobb a favoritnak
    és annál kisebb az underdog-nak a várható gól paramétere.
    """
    base_lambda = 1.25  # VB csoportkör átlag ~2.5 gól/meccs összesen

    # Logisztikus skálázás: 200 ELO pont ≈ kétszeres gólvárakozás
    scale = np.exp(elo_diff / 400.0)

    home_lambda = base_lambda * scale
    away_lambda = base_lambda / scale

    # Korlátok: ne legyen 0.1 alatt vagy 4.0 felett
    home_lambda = np.clip(home_lambda, 0.1, 4.0)
    away_lambda = np.clip(away_lambda, 0.1, 4.0)

    return home_lambda, away_lambda


def simulate_match_score(
    elo_diff: float,
    probs: np.ndarray,
    rng: np.random.Generator,
    is_knockout: bool = False,
) -> tuple[int, int, int, int | None, int | None]:
    """
    Szimulál egy meccs eredményt.

    Visszatér: (home_goals, away_goals, result_90min,
                home_goals_aet, away_goals_aet)
    ahol result_90min: 0=away, 1=draw, 2=home

    is_knockout=True esetén döntetlen nem lehetséges (hosszabbítás + büntetők).
    """
    away_p, draw_p, home_p = probs[0], probs[1], probs[2]

    # Poisson gólszámok generálása
    home_lambda, away_lambda = _expected_goals(elo_diff)
    home_goals = int(rng.poisson(home_lambda))
    away_goals = int(rng.poisson(away_lambda))

    # Az outcome valószínűségekhez igazítjuk a gólokat
    # (Poisson önmagában nem követi pontosan a W/D/L arányt)
    # Megoldjuk: az outcome-ot a modellből húzzuk, a gólszámot a Poisson-ból
    outcome = rng.choice([0, 1, 2], p=[away_p, draw_p, home_p])

    # Gólok korrekciója hogy egyezzenek az outcome-al
    if outcome == 2 and home_goals <= away_goals:
        home_goals = away_goals + rng.integers(1, 3)
    elif outcome == 0 and away_goals <= home_goals:
        away_goals = home_goals + rng.integers(1, 3)
    elif outcome == 1:
        # Döntetlen: egyenlővé tesszük a kisebbet a nagyobbhoz
        equal = min(home_goals, away_goals)
        home_goals = equal
        away_goals = equal

    # Kieséses: ha döntetlen → hosszabbítás → büntetők
    home_aet = home_goals
    away_aet = away_goals
    if is_knockout and home_goals == away_goals:
        # Hosszabbítás extra gól esélye ~30%
        if rng.random() < 0.3:
            if rng.random() < 0.5:
                home_aet += 1
            else:
                away_aet += 1

        # Ha még mindig döntetlen → büntetők (50/50 közelítő, de ELO súlyozva)
        if home_aet == away_aet:
            pen_home_win = 0.5 + elo_diff / 2000.0  # ±10% max
            pen_home_win = np.clip(pen_home_win, 0.35, 0.65)
            if rng.random() < pen_home_win:
                home_aet += 1
            else:
                away_aet += 1

    return home_goals, away_goals, outcome, home_aet, away_aet


# ── Csoportállás számítás ─────────────────────────────────────────────────────

class TeamStanding:
    def __init__(self, team: Team):
        self.team   = team
        self.pts    = 0
        self.gf     = 0   # goals for
        self.ga     = 0   # goals against
        self.wins   = 0

    @property
    def gd(self) -> int:
        return self.gf - self.ga

    def sort_key(self):
        return (self.pts, self.gd, self.gf, self.wins)


def simulate_group(
    group_teams: list[Team],
    probs_cache: dict[tuple[int, int], np.ndarray],
    elo_cache: dict[int, float],
    rng: np.random.Generator,
) -> list[Team]:
    """
    Szimulál egy csoportkört. Visszaadja a csapatokat rangsor szerint.
    """
    standings = {t.id: TeamStanding(t) for t in group_teams}

    # Round-robin: minden pár egyszer
    for i, home in enumerate(group_teams):
        for away in group_teams[i+1:]:
            probs    = probs_cache.get((home.id, away.id))
            if probs is None:
                probs = np.array([0.33, 0.33, 0.34])
            elo_diff = elo_cache.get(home.id, 1500) - elo_cache.get(away.id, 1500)

            hg, ag, outcome, _, _ = simulate_match_score(elo_diff, probs, rng, is_knockout=False)

            standings[home.id].gf += hg
            standings[home.id].ga += ag
            standings[away.id].gf += ag
            standings[away.id].ga += hg

            if outcome == 2:
                standings[home.id].pts += 3
                standings[home.id].wins += 1
            elif outcome == 1:
                standings[home.id].pts += 1
                standings[away.id].pts += 1
            else:
                standings[away.id].pts += 3
                standings[away.id].wins += 1

    ranked = sorted(standings.values(), key=lambda s: s.sort_key(), reverse=True)
    return [s.team for s in ranked]


# ── Kieséses meccs ────────────────────────────────────────────────────────────

def simulate_knockout_match(
    home: Team,
    away: Team,
    probs_cache: dict[tuple[int, int], np.ndarray],
    elo_cache: dict[int, float],
    rng: np.random.Generator,
) -> Team:
    """Szimulál egy kieséses meccset, visszaadja a győztest."""
    probs = probs_cache.get((home.id, away.id))
    if probs is None:
        # Fordítva sincs? → ELO alapú becslés
        elo_diff = elo_cache.get(home.id, 1500) - elo_cache.get(away.id, 1500)
        p_home = 1 / (1 + 10 ** (-elo_diff / 400))
        probs = np.array([1 - p_home - 0.05, 0.05, p_home])
        probs = np.clip(probs, 0.01, 0.98)
        probs /= probs.sum()

    elo_diff = elo_cache.get(home.id, 1500) - elo_cache.get(away.id, 1500)
    _, _, _, home_aet, away_aet = simulate_match_score(
        elo_diff, probs, rng, is_knockout=True
    )

    return home if home_aet > away_aet else away


# ── Teljes torna szimuláció ───────────────────────────────────────────────────

def simulate_tournament(
    groups: dict[str, list[Team]],
    probs_cache: dict[tuple[int, int], np.ndarray],
    elo_cache: dict[int, float],
    rng: np.random.Generator,
) -> dict[int, str]:
    """
    Szimulál egy teljes tornát.
    Visszaadja {team_id: legjobb_elért_szakasz} szótárt.
    """
    results: dict[int, str] = {}

    # ── Csoportkör ───────────────────────────────────────────────────
    group_rankings: dict[str, list[Team]] = {}
    for group_name, teams in groups.items():
        ranked = simulate_group(teams, probs_cache, elo_cache, rng)
        group_rankings[group_name] = ranked
        # 3. és 4. hely kiesik
        for t in ranked[2:]:
            results[t.id] = "Group stage"

    # ── Round of 32 párosítás (2026: 12 csoport, top 2 + legjobb 8 harmadik) ──
    # Egyszerűsítés: minden csoport top 2 + a 8 legjobb 3. helyezett
    group_names = sorted(group_rankings.keys())  # A-L

    # Top 2 minden csoportból
    r32_teams: list[Team] = []
    third_place: list[Team] = []

    for gn in group_names:
        r32_teams.append(group_rankings[gn][0])
        r32_teams.append(group_rankings[gn][1])
        third_place.append(group_rankings[gn][2])

    # Legjobb 8 harmadik: ELO alapján választjuk (pts helyett, mert pts nincs visszaadva)
    # Valóságban pontok alapján, de ELO közelítés jó elég a szimulációhoz
    third_by_elo = sorted(third_place, key=lambda t: elo_cache.get(t.id, 0), reverse=True)
    r32_teams.extend(third_by_elo[:8])

    for t in third_by_elo[8:]:
        results[t.id] = "Group stage"

    # ── Kieséses szakasz ──────────────────────────────────────────────
    stage_names = ["Round of 32", "Round of 16", "Quarter-final", "Semi-final", "Final"]
    current_round = r32_teams

    for stage in stage_names:
        next_round = []
        rng.shuffle(current_round)  # type: ignore  véletlen párosítás

        for i in range(0, len(current_round), 2):
            if i + 1 >= len(current_round):
                # Páratlan: bye (nem fordulhat elő ha a bracket helyes)
                next_round.append(current_round[i])
                continue

            home = current_round[i]
            away = current_round[i + 1]
            winner = simulate_knockout_match(home, away, probs_cache, elo_cache, rng)
            loser  = away if winner.id == home.id else home

            results[loser.id] = stage
            next_round.append(winner)

        current_round = next_round

    # Győztes
    if current_round:
        results[current_round[0].id] = "Winner"

    return results


# ── Monte Carlo futtatás ──────────────────────────────────────────────────────

def run_monte_carlo(
    conn: sqlite3.Connection,
    n_sims: int = N_SIMULATIONS,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Futtat n_sims torna szimulációt.
    Visszaadja a valószínűség táblázatot csapatonként.
    """
    if verbose:
        print(f"[sim] Előkészítés...")

    model, feature_names = load_model()

    groups     = load_2026_groups(conn)
    all_teams  = [t for ts in groups.values() for t in ts]
    team_ids   = {t.id for t in all_teams}

    # Jelenlegi ELO értékek
    elo_cache: dict[int, float] = {}
    for tid in team_ids:
        row = conn.execute(
            "SELECT elo_after FROM elo_log WHERE team_id=? ORDER BY id DESC LIMIT 1",
            (tid,)
        ).fetchone()
        elo_cache[tid] = row["elo_after"] if row else 1500.0

    # Feature gyorsítótár (elo timeline + forma)
    elo_tl     = _build_elo_timeline(conn)
    form_cache = _build_form_cache(conn)

    # Prediktor valószínűségek minden lehetséges párra (előre számolva)
    if verbose:
        print(f"[sim] Valószínűségek számítása ({len(all_teams)} csapat)...")

    probs_cache: dict[tuple[int, int], np.ndarray] = {}
    teams_list = list(team_ids)

    for i, h_id in enumerate(teams_list):
        for a_id in teams_list:
            if h_id == a_id:
                continue
            h_team = next((t for t in all_teams if t.id == h_id), None)
            a_team = next((t for t in all_teams if t.id == a_id), None)
            if not h_team or not a_team:
                continue

            try:
                X = build_prediction_row(
                    conn, h_id, a_id, "Group stage", elo_tl, form_cache
                )
                probs = predict_proba(model, feature_names, X)[0]
                probs_cache[(h_id, a_id)] = probs
            except Exception:
                elo_diff = elo_cache.get(h_id, 1500) - elo_cache.get(a_id, 1500)
                p_home = 1 / (1 + 10 ** (-elo_diff / 400))
                probs_cache[(h_id, a_id)] = np.array([1-p_home-0.05, 0.05, p_home])

    # ── Monte Carlo ────────────────────────────────────────────────────
    stages = ["Group stage", "Round of 32", "Round of 16",
              "Quarter-final", "Semi-final", "Final", "Winner"]

    # {team_id: {stage: count}}
    stage_counts: dict[int, dict[str, int]] = {
        tid: {s: 0 for s in stages} for tid in team_ids
    }

    if verbose:
        print(f"[sim] Monte Carlo ({n_sims:,} iteráció)...")

    rng = np.random.default_rng(RANDOM_SEED)

    for i in range(n_sims):
        sim_results = simulate_tournament(groups, probs_cache, elo_cache, rng)
        for team_id, best_stage in sim_results.items():
            if team_id in stage_counts:
                # Mindenki elér legalább a saját stagesig (kumulatív)
                reached_idx = stages.index(best_stage)
                for s in stages[:reached_idx + 1]:
                    stage_counts[team_id][s] += 1

        if verbose and (i + 1) % 2000 == 0:
            print(f"  {i+1:,}/{n_sims:,}")

    # ── Eredmény DataFrame ─────────────────────────────────────────────
    records = []
    for t in all_teams:
        counts = stage_counts[t.id]
        rec = {"team": t.name, "elo": round(elo_cache.get(t.id, 1500), 1)}
        for s in stages:
            rec[s] = round(counts[s] / n_sims * 100, 1)
        records.append(rec)

    df = pd.DataFrame(records).sort_values("Winner", ascending=False).reset_index(drop=True)
    df.index += 1
    return df


def print_results(df: pd.DataFrame) -> None:
    stages = ["Round of 32", "Round of 16", "Quarter-final",
              "Semi-final", "Final", "Winner"]

    print(f"\n{'='*85}")
    print(f"  FIFA VB 2026 – Monte Carlo Szimuláció (10 000 futtatás)")
    print(f"{'='*85}")
    print(f"{'#':>3} {'Csapat':<22} {'ELO':>6} {'R32':>6} {'R16':>6} {'QF':>6} {'SF':>6} {'Döntő':>7} {'Győz%':>7}")
    print(f"{'-'*85}")

    for i, row in df.iterrows():
        print(
            f"{i:>3} {row['team']:<22} {row['elo']:>6.0f} "
            f"{row['Round of 32']:>5.1f}% "
            f"{row['Round of 16']:>5.1f}% "
            f"{row['Quarter-final']:>5.1f}% "
            f"{row['Semi-final']:>5.1f}% "
            f"{row['Final']:>6.1f}% "
            f"{row['Winner']:>6.1f}%"
        )


if __name__ == "__main__":
    conn = get_connection()
    df = run_monte_carlo(conn, verbose=True)
    print_results(df)
    conn.close()