"""
simulation/simulate_2026.py

Teljes 2026-os VB szimuláció:
  1. Csoportkör – mind a 12 csoport lejátszva
  2. Csoportállások kinyomtatva
  3. A legjobb 8 harmadik meghatározása
  4. Round of 32 – a DB-beli bracket template alapján
  5. Round of 16 → Negyeddöntő → Elődöntő → Döntő

Futtatás:
  python simulation/simulate_2026.py
  python simulation/simulate_2026.py --seed 123   # reprodukálható eredmény
"""

import sys
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.db import get_connection
from pipeline import (
    build_prediction_row,
    _build_elo_timeline,
    _build_form_cache,
)
from train import load_model, predict_proba


# ─────────────────────────────────────────────────────────────────────────────
# Adatstruktúrák
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Team:
    id:   int
    name: str

    def __hash__(self):  return self.id
    def __eq__(self, o): return isinstance(o, Team) and self.id == o.id
    def __repr__(self):  return self.name


@dataclass
class Standing:
    team:  Team
    pts:   int = 0
    gf:    int = 0
    ga:    int = 0
    wins:  int = 0
    draws: int = 0

    @property
    def gd(self) -> int: return self.gf - self.ga

    def sort_key(self):
        return (self.pts, self.gd, self.gf, self.wins)


@dataclass
class MatchResult:
    home:       Team
    away:       Team
    home_goals: int
    away_goals: int
    winner:     Team   # döntetlen esetén None (csoportkörben)
    went_to_et: bool = False
    went_to_pens: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Valószínűség cache
# ─────────────────────────────────────────────────────────────────────────────

class ProbCache:
    """Előre kiszámolja és cache-eli a W/D/L valószínűségeket minden párra."""

    def __init__(self, conn, model, feature_names, elo_cache, teams):
        self._conn          = conn
        self._model         = model
        self._feature_names = feature_names
        self._elo_cache     = elo_cache
        self._elo_tl        = _build_elo_timeline(conn)
        self._form_cache    = _build_form_cache(conn)
        self._cache: dict[tuple[int,int], np.ndarray] = {}

        all_ids = list({t.id for t in teams})
        for h_id in all_ids:
            for a_id in all_ids:
                if h_id != a_id:
                    self._cache[(h_id, a_id)] = self._compute(h_id, a_id)

    def _compute(self, h_id: int, a_id: int) -> np.ndarray:
        try:
            X = build_prediction_row(
                self._conn, h_id, a_id, "Group stage",
                self._elo_tl, self._form_cache
            )
            return predict_proba(self._model, self._feature_names, X)[0]
        except Exception:
            elo_diff = self._elo_cache.get(h_id, 1500) - self._elo_cache.get(a_id, 1500)
            p_h = 1 / (1 + 10 ** (-elo_diff / 400))
            p_d = 0.22
            p_a = max(0.01, 1 - p_h - p_d)
            p_h = max(0.01, p_h - p_d / 2)
            arr = np.array([p_a, p_d, p_h])
            return arr / arr.sum()

    def get(self, h_id: int, a_id: int, stage: str = "Group stage") -> np.ndarray:
        if (h_id, a_id) in self._cache:
            probs = self._cache[(h_id, a_id)].copy()
            # Kieséses szakaszon nincs döntetlen → osszuk szét arányosan
            if _is_knockout(stage):
                away_p, draw_p, home_p = probs
                total_nodraw = away_p + home_p
                probs = np.array([
                    away_p + draw_p * (away_p / total_nodraw),
                    0.0,
                    home_p + draw_p * (home_p / total_nodraw),
                ])
            return probs
        
        # Fallback: ELO alapú
        elo_diff = self._elo_cache.get(h_id, 1500) - self._elo_cache.get(a_id, 1500)
        p_h = 1 / (1 + 10 ** (-elo_diff / 400))
        return np.array([1 - p_h - 0.05, 0.05, p_h])


def _is_knockout(stage: str) -> bool:
    s = stage.lower()
    return any(k in s for k in ["round", "quarter", "semi", "final"])


# ─────────────────────────────────────────────────────────────────────────────
# Meccs szimuláció
# ─────────────────────────────────────────────────────────────────────────────

def _expected_goals(elo_diff: float) -> tuple[float, float]:
    scale = np.exp(elo_diff / 400.0)
    return np.clip(1.25 * scale, 0.2, 4.0), np.clip(1.25 / scale, 0.2, 4.0)


def simulate_match(
    home: Team,
    away: Team,
    probs: np.ndarray,
    elo_diff: float,
    rng: np.random.Generator,
    knockout: bool = False,
) -> MatchResult:
    away_p, draw_p, home_p = probs[0], probs[1], probs[2]
    outcome = rng.choice([0, 1, 2], p=[away_p, draw_p, home_p])

    home_lam, away_lam = _expected_goals(elo_diff)
    hg = int(rng.poisson(home_lam))
    ag = int(rng.poisson(away_lam))

    # Gólok korrigálása az outcome-hoz
    # A régi gólkorrekció helyett használd ezt:
    while True:
        hg = int(rng.poisson(home_lam))
        ag = int(rng.poisson(away_lam))

        if outcome == 2 and hg > ag: break     # Hazai győzelem
        if outcome == 0 and hg < ag: break     # Vendég győzelem
        if outcome == 1 and hg == ag: break    # Döntetlen

    went_et = went_pens = False

    if knockout and hg == ag:
        went_et = True
        # Hosszabbítás: 30 perces meccs, az eredeti lambdák ~1/3-ával
        et_hg = int(rng.poisson(home_lam / 3.0))
        et_ag = int(rng.poisson(away_lam / 3.0))
        hg += et_hg
        ag += et_ag

        if hg == ag:  # Még mindig döntetlen -> büntetők
            went_pens = True
            # A büntetőpárbaj sokkal közelebb van az 50-50%-hoz.
            # Max 55-45%-ra billenjen a jobbik javára.
            elo_diff_clipped = np.clip(elo_diff, -200, 200)
            p_home_pen = 0.5 + (elo_diff_clipped / 4000.0) 
            if rng.random() < p_home_pen:
                hg += 1
            else:
                ag += 1

    winner = home if hg > ag else away
    return MatchResult(home, away, hg, ag, winner, went_et, went_pens)


# ─────────────────────────────────────────────────────────────────────────────
# Csoportkör
# ─────────────────────────────────────────────────────────────────────────────

def simulate_group(
    group_name: str,
    teams: list[Team],
    matches: list[dict],         # DB sorok: home_id, away_id
    cache: ProbCache,
    elo_cache: dict[int, float],
    rng: np.random.Generator,
) -> tuple[dict[int, Standing], list[MatchResult]]:
    standings = {t.id: Standing(t) for t in teams}
    results   = []

    for m in matches:
        home = next(t for t in teams if t.id == m["home_id"])
        away = next(t for t in teams if t.id == m["away_id"])
        probs    = cache.get(home.id, away.id)
        elo_diff = elo_cache.get(home.id, 1500) - elo_cache.get(away.id, 1500)

        res = simulate_match(home, away, probs, elo_diff, rng, knockout=False)
        results.append(res)

        standings[home.id].gf += res.home_goals
        standings[home.id].ga += res.away_goals
        standings[away.id].gf += res.away_goals
        standings[away.id].ga += res.home_goals

        if res.home_goals > res.away_goals:
            standings[home.id].pts  += 3
            standings[home.id].wins += 1
        elif res.home_goals == res.away_goals:
            standings[home.id].pts   += 1
            standings[away.id].pts   += 1
            standings[home.id].draws += 1
            standings[away.id].draws += 1
        else:
            standings[away.id].pts  += 3
            standings[away.id].wins += 1

    return standings, results


# ─────────────────────────────────────────────────────────────────────────────
# Harmadik helyezettek rangsorolása (2026: 12 csoportból 8 megy tovább)
# ─────────────────────────────────────────────────────────────────────────────

def best_third_place(
    third_standings: list[tuple[str, Standing]]   # [(group_name, standing), ...]
) -> tuple[list[tuple[str, Standing]], list[tuple[str, Standing]]]:
    """
    Visszaadja (továbbjutó_8, kiesett_4) sorban.
    Rendezés: pontok → gólkülönbség → lőtt gólok → véletlen (tiebreak).
    """
    ranked = sorted(
        third_standings,
        key=lambda x: (x[1].pts, x[1].gd, x[1].gf),
        reverse=True
    )
    return ranked[:8], ranked[8:]


# ─────────────────────────────────────────────────────────────────────────────
# Round of 32 bracket feloldása
# ─────────────────────────────────────────────────────────────────────────────

def resolve_r32_bracket(
    r32_template: list[dict],       # DB sorok: home="1A", away="2B" stb.
    group_results: dict[str, list[Standing]],   # "A" → [1., 2., 3., 4.]
    best_thirds: list[tuple[str, Standing]],    # [(group, standing), ...]
) -> list[tuple[Team, Team]]:
    """
    Feloldja a placeholder neveket valódi csapatokra.
    Pl. "1A" → csoport A 1. helyezettje
        "3A/B/C/D/F" → a legjobb harmadikak közül aki az A/B/C/D/F csoportból van
    """
    # Lookup: "1A" → Team
    slot_to_team: dict[str, Team] = {}

    for group_letter, standings in group_results.items():
        slot_to_team[f"1{group_letter}"] = standings[0].team
        slot_to_team[f"2{group_letter}"] = standings[1].team
        slot_to_team[f"3{group_letter}"] = standings[2].team

    # Harmadik helyezett slot-ok feloldása
    # A DB-ben pl. "3A/B/C/D/F" azt jelenti: az A,B,C,D,F csoportok
    # harmadikjai közül az a legjobb, aki továbbjutott
    remaining_thirds = list(best_thirds)   # [(group_name, standing)]

    def resolve_third_slot(slot: str) -> Team:
        """slot pl. '3A/B/C/D/F' → a legjobb harmadik abból a halmazból"""
        groups_in_slot = slot[1:].split("/")   # ["A","B","C","D","F"]
        # Keres a remaining_thirds-ben
        for i, (gname, st) in enumerate(remaining_thirds):
            if gname in groups_in_slot:
                remaining_thirds.pop(i)
                return st.team
        # Fallback: első maradt
        if remaining_thirds:
            return remaining_thirds.pop(0)[1].team
        raise ValueError(f"Nem találtam harmadik helyezettet: {slot}")

    pairs: list[tuple[Team, Team]] = []
    for m in sorted(r32_template, key=lambda x: x["match_date"]):
        home_slot = m["home_name"]
        away_slot = m["away_name"]

        if home_slot.startswith("3"):
            home_team = resolve_third_slot(home_slot)
        else:
            home_team = slot_to_team.get(home_slot)

        if away_slot.startswith("3"):
            away_team = resolve_third_slot(away_slot)
        else:
            away_team = slot_to_team.get(away_slot)

        if home_team and away_team:
            pairs.append((home_team, away_team))

    return pairs


# ─────────────────────────────────────────────────────────────────────────────
# Kieséses szakasz
# ─────────────────────────────────────────────────────────────────────────────

def simulate_knockout_round(
    pairs: list[tuple[Team, Team]],
    stage_name: str,
    cache: ProbCache,
    elo_cache: dict[int, float],
    rng: np.random.Generator,
) -> tuple[list[Team], list[MatchResult]]:
    """Szimulál egy teljes kieséses kört. Visszaadja a győzteseket és eredményeket."""
    winners = []
    results = []

    for home, away in pairs:
        probs    = cache.get(home.id, away.id, stage_name)
        elo_diff = elo_cache.get(home.id, 1500) - elo_cache.get(away.id, 1500)
        res = simulate_match(home, away, probs, elo_diff, rng, knockout=True)
        results.append(res)
        winners.append(res.winner)

    return winners, results


# ─────────────────────────────────────────────────────────────────────────────
# Kiíró függvények
# ─────────────────────────────────────────────────────────────────────────────

W  = "\033[1;32m"   # zöld (továbbjutó)
Y  = "\033[1;33m"   # sárga (harmadik)
R  = "\033[0;31m"   # piros (kiesett)
B  = "\033[1;34m"   # kék (kiemelés)
DIM = "\033[2m"
RESET = "\033[0m"


def _flag(name: str) -> str:
    flags = {
        "France":"🇫🇷","Argentina":"🇦🇷","Brazil":"🇧🇷","Germany":"🇩🇪",
        "England":"🏴󠁧󠁢󠁥󠁮󠁧󠁿","Spain":"🇪🇸","Netherlands":"🇳🇱","Portugal":"🇵🇹",
        "Belgium":"🇧🇪","Croatia":"🇭🇷","Japan":"🇯🇵","Morocco":"🇲🇦",
        "USA":"🇺🇸","Mexico":"🇲🇽","Canada":"🇨🇦","Uruguay":"🇺🇾",
        "Colombia":"🇨🇴","Ecuador":"🇪🇨","Senegal":"🇸🇳","South Korea":"🇰🇷",
        "Australia":"🇦🇺","Switzerland":"🇨🇭","Denmark":"🇩🇰","Poland":"🇵🇱",
        "Serbia":"🇷🇸","Iran":"🇮🇷","Saudi Arabia":"🇸🇦","Ghana":"🇬🇭",
        "Cameroon":"🇨🇲","Tunisia":"🇹🇳","Qatar":"🇶🇦","Turkey":"🇹🇷",
        "Sweden":"🇸🇪","Norway":"🇳🇴","Austria":"🇦🇹","Czech Republic":"🇨🇿",
        "Slovakia":"🇸🇰","Scotland":"🏴󠁧󠁢󠁳󠁣󠁴󠁿","Wales":"🏴󠁧󠁢󠁷󠁬󠁳󠁿","Algeria":"🇩🇿",
        "Nigeria":"🇳🇬","Egypt":"🇪🇬","South Africa":"🇿🇦","Paraguay":"🇵🇾",
        "Bolivia":"🇧🇴","Peru":"🇵🇪","Chile":"🇨🇱","Costa Rica":"🇨🇷",
        "Panama":"🇵🇦","Honduras":"🇭🇳","Haiti":"🇭🇹","Jamaica":"🇯🇲",
        "Cuba":"🇨🇺","Curaçao":"🇨🇼","New Zealand":"🇳🇿","Uzbekistan":"🇺🇿",
        "Jordan":"🇯🇴","Iraq":"🇮🇶","Bosnia & Herzegovina":"🇧🇦",
        "Bosnia and Herzegovina":"🇧🇦","Cape Verde":"🇨🇻","DR Congo":"🇨🇩",
        "Ivory Coast":"🇨🇮","Portugal":"🇵🇹",
    }
    return flags.get(name, "🏳")


def print_group_standings(
    group_name: str,
    standings: list[Standing],
    results: list[MatchResult],
) -> None:
    print(f"\n  ┌─── {group_name} {'─'*(28-len(group_name))}┐")
    print(f"  │  {'Csapat':<22} Pts  GY  D  V  LG  KG  GK │")
    print(f"  ├{'─'*48}┤")

    for i, s in enumerate(standings):
        flag = _flag(s.team.name)
        name = s.team.name[:20]
        gv   = s.wins
        d    = s.draws
        v    = s.pts//1 - s.wins*3 - s.draws  # vereség
        v    = max(0, 3 - gv - d)              # 3 mérkőzésből

        if i == 0:
            color = W     # 1. hely
        elif i == 1:
            color = W     # 2. hely
        elif i == 2:
            color = Y     # 3. hely (esetleg továbbjut)
        else:
            color = DIM   # kiesett

        print(
            f"  │{color} {i+1}. {flag} {name:<20}{RESET}"
            f"{color}{s.pts:>3}  {gv:>2}  {d:>1}  {v:>1}  "
            f"{s.gf:>2}  {s.ga:>2}  {s.gd:>+3}{RESET} │"
        )

    print(f"  └{'─'*48}┘")

    # Meccs eredmények
    print(f"  {DIM}  Eredmények:{RESET}")
    for r in results:
        suffix = ""
        if r.went_to_pens: suffix = " (b)"
        elif r.went_to_et: suffix = " (h)"
        print(
            f"    {DIM}{_flag(r.home.name)} {r.home.name:<22} "
            f"{r.home_goals}–{r.away_goals}  "
            f"{r.away.name:<22} {_flag(r.away.name)}{suffix}{RESET}"
        )


def print_knockout_round(
    stage_name: str,
    pairs: list[tuple[Team, Team]],
    results: list[MatchResult],
) -> None:
    width = 60
    print(f"\n  {'═'*width}")
    print(f"  {B}  ▶  {stage_name.upper()}{RESET}")
    print(f"  {'═'*width}")

    for res in results:
        h_flag = _flag(res.home.name)
        a_flag = _flag(res.away.name)
        suffix = ""
        if res.went_to_pens: suffix = f"  {DIM}(büntetők){RESET}"
        elif res.went_to_et: suffix = f"  {DIM}(hosszabbítás){RESET}"

        win_color  = W
        lose_color = DIM

        if res.winner == res.home:
            h_col, a_col = win_color, lose_color
        else:
            h_col, a_col = lose_color, win_color

        print(
            f"  {h_col}{h_flag} {res.home.name:<22}{RESET}"
            f"  {res.home_goals}–{res.away_goals}  "
            f"{a_col}{res.away.name:<22} {a_flag}{RESET}"
            f"{suffix}"
        )


def print_header() -> None:
    print()
    print("  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║         🏆  FIFA WORLD CUP 2026 – SZIMULÁCIÓ  🏆            ║")
    print("  ║             USA / Canada / Mexico                             ║")
    print("  ╚══════════════════════════════════════════════════════════════╝")


def print_champion(winner: Team) -> None:
    flag  = _flag(winner.name)
    line  = f"  🏆  GYŐZTES:  {flag}  {winner.name}  {flag}  🏆"
    pad   = "═" * (len(line) - 10)  # emoji kompenzáció
    print(f"\n  {W}{'═'*58}{RESET}")
    print(f"  {W}{line}{RESET}")
    print(f"  {W}{'═'*58}{RESET}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Fő futtatás
# ─────────────────────────────────────────────────────────────────────────────

def run(seed: int = 42) -> None:
    conn = get_connection()
    rng  = np.random.default_rng(seed)

    # ── Modell és cache betöltés ──────────────────────────────────────
    model, feature_names = load_model()

    # Minden 2026-os csapat
    rows = conn.execute("""
        SELECT DISTINCT t.id, t.name
        FROM team t
        JOIN match m ON (m.home_team_id=t.id OR m.away_team_id=t.id)
        JOIN tournament tr ON tr.id=m.tournament_id
        WHERE tr.year=2026 AND m.stage LIKE 'Group %' AND m.stage != 'Group stage'
    """).fetchall()
    all_teams = [Team(r["id"], r["name"]) for r in rows]

    elo_cache: dict[int, float] = {}
    for t in all_teams:
        row = conn.execute(
            "SELECT elo_after FROM elo_log WHERE team_id=? ORDER BY id DESC LIMIT 1",
            (t.id,)
        ).fetchone()
        elo_cache[t.id] = row["elo_after"] if row else 1500.0

    cache = ProbCache(conn, model, feature_names, elo_cache, all_teams)

    # ── Csoportok és meccsek betöltése ────────────────────────────────
    group_letters = [chr(ord("A") + i) for i in range(12)]

    groups:        dict[str, list[Team]]     = {}
    group_matches: dict[str, list[dict]]     = {}

    for gl in group_letters:
        stage = f"Group {gl}"
        rows  = conn.execute("""
            SELECT h.id as home_id, h.name as home_name,
                   a.id as away_id, a.name as away_name,
                   m.match_date
            FROM match m
            JOIN team h ON h.id=m.home_team_id
            JOIN team a ON a.id=m.away_team_id
            JOIN tournament t ON t.id=m.tournament_id
            WHERE t.year=2026 AND m.stage=?
            ORDER BY m.match_date
        """, (stage,)).fetchall()

        if not rows:
            continue

        team_ids_in_group: set[int] = set()
        matches = []
        for r in rows:
            team_ids_in_group.add(r["home_id"])
            team_ids_in_group.add(r["away_id"])
            matches.append(dict(r))

        groups[gl]        = [t for t in all_teams if t.id in team_ids_in_group]
        group_matches[gl] = matches

    # ── R32 bracket template ──────────────────────────────────────────
    r32_rows = conn.execute("""
        SELECT h.name as home_name, a.name as away_name, m.match_date
        FROM match m
        JOIN team h ON h.id=m.home_team_id
        JOIN team a ON a.id=m.away_team_id
        JOIN tournament t ON t.id=m.tournament_id
        WHERE t.year=2026 AND m.stage='Round of 32'
        ORDER BY m.match_date
    """).fetchall()
    r32_template = [dict(r) for r in r32_rows]

    # ══════════════════════════════════════════════════════════════════
    print_header()

    # ── 1. CSOPORTKÖR ─────────────────────────────────────────────────
    print(f"\n  {'─'*60}")
    print(f"  {B}  ▶  CSOPORTKÖR{RESET}")
    print(f"  {'─'*60}")

    group_standings: dict[str, list[Standing]] = {}
    third_place_list: list[tuple[str, Standing]] = []

    for gl in sorted(groups.keys()):
        standings_dict, results = simulate_group(
            f"Group {gl}",
            groups[gl],
            group_matches[gl],
            cache, elo_cache, rng,
        )
        ranked = sorted(standings_dict.values(), key=lambda s: s.sort_key(), reverse=True)
        group_standings[gl] = ranked
        third_place_list.append((gl, ranked[2]))

        print_group_standings(f"Group {gl}", ranked, results)

    # ── 2. HARMADIK HELYEZETTEK ───────────────────────────────────────
    best_thirds, eliminated_thirds = best_third_place(third_place_list)

    print(f"\n  {'─'*60}")
    print(f"  {B}  ▶  LEGJOBB 8 HARMADIK HELYEZETT{RESET}")
    print(f"  {'─'*60}")
    for gl, st in best_thirds:
        print(f"    {W}✓{RESET} {_flag(st.team.name)} {st.team.name:<22} "
              f"(Group {gl})  Pts:{st.pts}  GK:{st.gd:+d}")
    print(f"  {DIM}  Kiesett harmadikak:{RESET}")
    for gl, st in eliminated_thirds:
        print(f"    {DIM}✗ {st.team.name} (Group {gl})  Pts:{st.pts}{RESET}")

    # ── 3. ROUND OF 32 ────────────────────────────────────────────────
    r32_pairs = resolve_r32_bracket(r32_template, group_standings, best_thirds)

    r32_winners, r32_results = simulate_knockout_round(
        r32_pairs, "Round of 32", cache, elo_cache, rng
    )
    print_knockout_round("Round of 32", r32_pairs, r32_results)

    # ── 4. ROUND OF 16 ────────────────────────────────────────────────
    r16_pairs = [(r32_winners[i], r32_winners[i+1]) for i in range(0, len(r32_winners), 2)]
    r16_winners, r16_results = simulate_knockout_round(
        r16_pairs, "Round of 16", cache, elo_cache, rng
    )
    print_knockout_round("Round of 16", r16_pairs, r16_results)

    # ── 5. NEGYEDDÖNTŐ ────────────────────────────────────────────────
    qf_pairs = [(r16_winners[i], r16_winners[i+1]) for i in range(0, len(r16_winners), 2)]
    qf_winners, qf_results = simulate_knockout_round(
        qf_pairs, "Quarter-final", cache, elo_cache, rng
    )
    print_knockout_round("Quarter-final", qf_pairs, qf_results)

    # ── 6. ELŐDÖNTŐ ───────────────────────────────────────────────────
    sf_pairs = [(qf_winners[i], qf_winners[i+1]) for i in range(0, len(qf_winners), 2)]
    sf_winners, sf_results = simulate_knockout_round(
        sf_pairs, "Semi-final", cache, elo_cache, rng
    )
    print_knockout_round("Semi-final", sf_pairs, sf_results)

    # ── 7. DÖNTŐ ──────────────────────────────────────────────────────
    if len(sf_winners) >= 2:
        final_pairs = [(sf_winners[0], sf_winners[1])]
        final_winners, final_results = simulate_knockout_round(
            final_pairs, "Final", cache, elo_cache, rng
        )
        print_knockout_round("Final", final_pairs, final_results)
        print_champion(final_winners[0])

    conn.close()


if __name__ == "__main__":
    seed = 42
    if "--seed" in sys.argv:
        idx  = sys.argv.index("--seed")
        seed = int(sys.argv[idx + 1])

    run(seed=seed)