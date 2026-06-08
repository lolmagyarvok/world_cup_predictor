"""
features/pipeline.py

Feature mátrix összerakása a DB-ből modell tanításhoz és predikcióhoz.

Feature-ök (meccs-szintű, mindkét csapatra szimmetrikusan):
  ELO alapú (mindig elérhető):
    - elo_diff            : home_elo - away_elo a meccs előtt
    - elo_home            : home csapat abszolút ELO-ja
    - elo_away            : away csapat abszolút ELO-ja

  Forma (elo_log-ból számolt, utolsó N meccs):
    - home_win_rate_last5 : utolsó 5 meccs győzelmi arány
    - away_win_rate_last5
    - home_momentum       : ELO változás az utolsó 5 meccsben
    - away_momentum

  Torna kontextus:
    - is_knockout         : 1 ha kieséses szakasz
    - stage_weight        : 1.0 (csoport) .. 1.25 (döntő)
    - elo_home_rank       : ELO alapú relatív erősség (0..1)

  Opcionális (ha CSV be van töltve):
    - fifa_rank_diff      : home_rank - away_rank
    - squad_value_diff    : home_value - away_value (EUR)
    - form_goals_diff     : goals_scored_last_4y diff
    - experience_diff     : world_cup_participations diff

  Célváltozó (label):
    - outcome: 0=away győz, 1=döntetlen, 2=home győz
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import get_connection

LAST_N = 5   # utolsó N meccs a forma számításhoz


# ── ELO snapshotok (meccs előtt) ──────────────────────────────────────────────

def _build_elo_timeline(conn: sqlite3.Connection) -> dict[tuple[int, int], float]:
    """
    Visszaadja {(team_id, match_id): elo_before} szótárt az elo_log-ból.
    Pontosan a meccs ELŐTTI ELO értéket adja vissza.
    """
    rows = conn.execute(
        "SELECT team_id, match_id, elo_before FROM elo_log"
    ).fetchall()
    return {(r["team_id"], r["match_id"]): r["elo_before"] for r in rows}


def _build_form_cache(conn: sqlite3.Connection) -> dict[tuple[int, int], dict]:
    """
    {(team_id, match_id): {'win_rate': float, 'momentum': float}}

    win_rate  = az adott meccs ELŐTTI utolsó LAST_N meccs győzelmi aránya
    momentum  = ELO változás az utolsó LAST_N meccsben
    """
    # Összes elo_log sor időrendben
    rows = conn.execute("""
        SELECT el.team_id, el.match_id, el.elo_before, el.elo_after,
               m.match_date, m.home_team_id, m.away_team_id,
               m.home_score, m.away_score
        FROM elo_log el
        JOIN match m ON m.id = el.match_id
        ORDER BY m.match_date ASC, el.match_id ASC
    """).fetchall()

    # Csapatonként rendezett meccs lista
    from collections import defaultdict
    team_history: dict[int, list[dict]] = defaultdict(list)

    for r in rows:
        team_id  = r["team_id"]
        is_home  = (r["home_team_id"] == team_id)
        h_sc, a_sc = r["home_score"], r["away_score"]

        if h_sc is None or a_sc is None:
            win = 0.5
        elif is_home:
            win = 1.0 if h_sc > a_sc else (0.5 if h_sc == a_sc else 0.0)
        else:
            win = 1.0 if a_sc > h_sc else (0.5 if h_sc == a_sc else 0.0)

        team_history[team_id].append({
            "match_id":   r["match_id"],
            "elo_before": r["elo_before"],
            "elo_after":  r["elo_after"],
            "win":        win,
        })

    cache: dict[tuple[int, int], dict] = {}

    for team_id, history in team_history.items():
        for i, entry in enumerate(history):
            past = history[max(0, i - LAST_N):i]  # ELŐTTE lévő meccsek
            if not past:
                win_rate = 0.5
                momentum = 0.0
            else:
                win_rate = np.mean([p["win"] for p in past])
                momentum = past[-1]["elo_after"] - past[0]["elo_before"]

            cache[(team_id, entry["match_id"])] = {
                "win_rate": win_rate,
                "momentum": momentum,
            }

    return cache


def _outcome_label(home_score: int, away_score: int) -> int:
    """0=away, 1=döntetlen, 2=home"""
    if home_score > away_score:
        return 2
    if home_score == away_score:
        return 1
    return 0


STAGE_WEIGHTS = {
    "final":            1.25,
    "semi":             1.15,
    "quarter":          1.10,
    "round of 16":      1.05,
    "round of 32":      1.02,
}

def _stage_weight(stage: str) -> float:
    s = stage.lower()
    for key, val in STAGE_WEIGHTS.items():
        if key in s:
            return val
    return 1.0

def _is_knockout(stage: str) -> int:
    s = stage.lower()
    return int(any(k in s for k in ["final", "semi", "quarter", "round of"]))


# ── fő API ────────────────────────────────────────────────────────────────────

def build_training_matrix(
    conn: sqlite3.Connection,
    years: list[int] | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Összerakja a tanítóadatokat.

    Visszatér: (X, y) ahol
      X : pd.DataFrame  – feature mátrix
      y : pd.Series     – outcome (0/1/2)
    """
    if years is None:
        years = [2002, 2006, 2010, 2014, 2018, 2022]

    year_ph = ",".join("?" * len(years))
    matches = conn.execute(f"""
        SELECT
            m.id, m.home_team_id, m.away_team_id,
            m.home_score, m.away_score,
            m.stage, m.match_date, m.elo_diff_pre,
            t.year AS tournament_year,
            tts_h.fifa_rank_pre       AS home_fifa_rank,
            tts_a.fifa_rank_pre       AS away_fifa_rank,
            tts_h.squad_total_value_eur AS home_value,
            tts_a.squad_total_value_eur AS away_value,
            tts_h.goals_scored_last_4y  AS home_gs4y,
            tts_a.goals_scored_last_4y  AS away_gs4y,
            tts_h.world_cup_participations_before AS home_exp,
            tts_a.world_cup_participations_before AS away_exp,
            tts_h.is_host             AS home_is_host,
            tts_a.is_host             AS away_is_host
        FROM match m
        JOIN tournament t ON t.id = m.tournament_id
        LEFT JOIN team_tournament_stat tts_h
            ON tts_h.team_id = m.home_team_id AND tts_h.tournament_id = m.tournament_id
        LEFT JOIN team_tournament_stat tts_a
            ON tts_a.team_id = m.away_team_id AND tts_a.tournament_id = m.tournament_id
        WHERE t.year IN ({year_ph})
          AND m.home_score IS NOT NULL
          AND m.away_score IS NOT NULL
          AND m.elo_diff_pre IS NOT NULL
        ORDER BY m.match_date ASC
    """, years).fetchall()

    elo_tl    = _build_elo_timeline(conn)
    form_cache = _build_form_cache(conn)

    records = []
    for m in matches:
        mid     = m["id"]
        home_id = m["home_team_id"]
        away_id = m["away_team_id"]

        elo_home = elo_tl.get((home_id, mid), 1500.0)
        elo_away = elo_tl.get((away_id, mid), 1500.0)

        home_form = form_cache.get((home_id, mid), {"win_rate": 0.5, "momentum": 0.0})
        away_form = form_cache.get((away_id, mid), {"win_rate": 0.5, "momentum": 0.0})

        # Összes ELO max normalizáláshoz (relatív erősség)
        elo_max = max(elo_home, elo_away, 1.0)

        rec: dict = {
            # Core ELO
            "elo_diff":           m["elo_diff_pre"],
            "elo_home":           elo_home,
            "elo_away":           elo_away,
            "elo_home_rel":       elo_home / elo_max,
            # Forma
            "home_win_rate":      home_form["win_rate"],
            "away_win_rate":      away_form["win_rate"],
            "win_rate_diff":      home_form["win_rate"] - away_form["win_rate"],
            "home_momentum":      home_form["momentum"],
            "away_momentum":      away_form["momentum"],
            "momentum_diff":      home_form["momentum"] - away_form["momentum"],
            # Kontextus
            "is_knockout":        _is_knockout(m["stage"] or ""),
            "stage_weight":       _stage_weight(m["stage"] or ""),
            "is_host_home":       int(m["home_is_host"] or 0),
            "is_host_away":       int(m["away_is_host"] or 0),
        }

        # Opcionális (CSV) feature-ök – NULL ha nincs adat
        hr = m["home_fifa_rank"]
        ar = m["away_fifa_rank"]
        rec["fifa_rank_diff"]   = (hr - ar)         if (hr and ar) else 0.0
        rec["squad_value_diff"] = ((m["home_value"] or 0) - (m["away_value"] or 0)) / 1e6
        rec["form_goals_diff"]  = ((m["home_gs4y"] or 0) - (m["away_gs4y"] or 0))
        rec["experience_diff"]  = ((m["home_exp"] or 0)  - (m["away_exp"] or 0))

        rec["_outcome"] = _outcome_label(m["home_score"], m["away_score"])
        records.append(rec)

    df = pd.DataFrame(records)
    X  = df.drop(columns=["_outcome"])
    y  = df["_outcome"]
    return X, y


def build_prediction_row(
    conn: sqlite3.Connection,
    home_team_id: int,
    away_team_id: int,
    stage: str = "Group stage",
    elo_tl: dict | None = None,
    form_cache: dict | None = None,
) -> pd.DataFrame:
    """
    Egyetlen meccs feature vektorát rakja össze predikcihoz.
    elo_tl és form_cache átadható ha batch predikcióhoz előre számolod.
    """
    if elo_tl is None:
        elo_tl = _build_elo_timeline(conn)
    if form_cache is None:
        form_cache = _build_form_cache(conn)

    # Legutóbbi ELO értékek
    home_elo_rows = conn.execute("""
        SELECT elo_after FROM elo_log WHERE team_id=?
        ORDER BY id DESC LIMIT 1
    """, (home_team_id,)).fetchone()
    away_elo_rows = conn.execute("""
        SELECT elo_after FROM elo_log WHERE team_id=?
        ORDER BY id DESC LIMIT 1
    """, (away_team_id,)).fetchone()

    elo_home = home_elo_rows["elo_after"] if home_elo_rows else 1500.0
    elo_away = away_elo_rows["elo_after"] if away_elo_rows else 1500.0

    # Legutóbbi forma – utolsó match_id alapján
    last_home_match = conn.execute("""
        SELECT match_id FROM elo_log WHERE team_id=? ORDER BY id DESC LIMIT 1
    """, (home_team_id,)).fetchone()
    last_away_match = conn.execute("""
        SELECT match_id FROM elo_log WHERE team_id=? ORDER BY id DESC LIMIT 1
    """, (away_team_id,)).fetchone()

    home_form = {"win_rate": 0.5, "momentum": 0.0}
    away_form = {"win_rate": 0.5, "momentum": 0.0}
    if last_home_match:
        home_form = form_cache.get((home_team_id, last_home_match["match_id"]), home_form)
    if last_away_match:
        away_form = form_cache.get((away_team_id, last_away_match["match_id"]), away_form)

    elo_max = max(elo_home, elo_away, 1.0)

    # tts adatok a 2026-os tornára
    tts_q = """
        SELECT fifa_rank_pre, squad_total_value_eur,
               goals_scored_last_4y, world_cup_participations_before, is_host
        FROM team_tournament_stat tts
        JOIN tournament t ON t.id=tts.tournament_id
        WHERE tts.team_id=? AND t.year=2026
    """
    home_tts = conn.execute(tts_q, (home_team_id,)).fetchone()
    away_tts = conn.execute(tts_q, (away_team_id,)).fetchone()

    def _tts(row, col, default=0):
        return row[col] if (row and row[col] is not None) else default

    rec = {
        "elo_diff":           elo_home - elo_away,
        "elo_home":           elo_home,
        "elo_away":           elo_away,
        "elo_home_rel":       elo_home / elo_max,
        "home_win_rate":      home_form["win_rate"],
        "away_win_rate":      away_form["win_rate"],
        "win_rate_diff":      home_form["win_rate"] - away_form["win_rate"],
        "home_momentum":      home_form["momentum"],
        "away_momentum":      away_form["momentum"],
        "momentum_diff":      home_form["momentum"] - away_form["momentum"],
        "is_knockout":        _is_knockout(stage),
        "stage_weight":       _stage_weight(stage),
        "is_host_home":       _tts(home_tts, "is_host"),
        "is_host_away":       _tts(away_tts, "is_host"),
        "fifa_rank_diff":     _tts(home_tts, "fifa_rank_pre") - _tts(away_tts, "fifa_rank_pre"),
        "squad_value_diff":   (_tts(home_tts, "squad_total_value_eur") - _tts(away_tts, "squad_total_value_eur")) / 1e6,
        "form_goals_diff":    _tts(home_tts, "goals_scored_last_4y") - _tts(away_tts, "goals_scored_last_4y"),
        "experience_diff":    _tts(home_tts, "world_cup_participations_before") - _tts(away_tts, "world_cup_participations_before"),
    }
    return pd.DataFrame([rec])


if __name__ == "__main__":
    conn = get_connection()
    print("[pipeline] Feature mátrix építése...")
    X, y = build_training_matrix(conn)
    print(f"  Shape: {X.shape}")
    print(f"  Label eloszlás: {y.value_counts().to_dict()}")
    print(f"  Feature-ök: {list(X.columns)}")
    print(f"\n  Minta (első 3 sor):\n{X.head(3).to_string()}")
    conn.close()