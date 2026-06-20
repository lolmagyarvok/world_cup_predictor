
# VB Predictor — Projekt dokumentáció

## A projekt célja

A **VB Predictor** egy gépi tanulásalapú rendszer, amely a **FIFA Világbajnokság (2026)** mérkőzéseinek kimenetelét jósolja meg, és napi szintű fogadási ajánlásokat (expected value alapú tippeket) generál.

**Főbb funkciók:**
1. Több forrásból származó adatgyűjtés (történelmi VB meccsek, válogatott meccsek 2002-től, StatsBomb, API-Football)
2. Saját **ELO** számítás az összes válogatott meccsre (K-faktor torna típusonként változó)
3. **XGBoost** osztályozó betanítása 18 feature segítségével (ELO, forma, torna kontextus, FIFA rang, keretérték, tapasztalat)
4. **Napi automatizált pipeline**: eredményfrissítés → kiértékelés → új predikciók
5. **Teljes 2026 VB szimuláció** csoportkör → egyenes kiesés → döntő
6. ROI és pontosság mérés a valós eredmények alapján

---

## Mappaszerkezet és fájlok

### Gyökérkönyvtár

| Fájl/Mappa | Leírás |
|---|---|
| `modell/` | A **gépi tanulási modell** központi mappája (lásd alább) |
| `data/` | **Adatbetöltők** és API kliensek (lásd alább) |
| `scripts/` | **Napi üzemeltetés** szkriptjei |
| `database/` | **Adatbázis séma** (`schema.sql`) és maga az SQLite fájl (`worldcup_database.db`) |
| `accuracy checker/` | **Offline pontosság-ellenőrző** a szimulációhoz |
| `logs/` | Napi pipeline logok ide kerülnek |
| `data/api_cache/` | API válaszok cache-elve (JSON fájlok) |
| `run_daily.sh` | **Napi ütemezett futtatás** bash szkriptje (cron-hoz) |
| `.env` | **API kulcsok** (API-Football, Odds API) |
| `requirements.txt` | Python függőségek |
| `simulation_predictions.json` | Szimuláció kimeneti fájlja |
| `élmény.txt` | Fejlesztői jegyzetek (ismert bugfixek) |
| `building diagram.png` | Architektúra diagram |

### `modell/` — Gépi tanulási modell

| Fájl | Leírás |
|---|---|
| `train.py` | **XGBoost** osztályozó (W/D/L) + **CalibratedClassifierCV** (isotonic kalibrálás). **TimeSeriesSplit** (5 fold) keresztvalidáció, idő alapú split (nem szivárog jövő a múltba). Eredmény: `xgb_model.joblib`, `feature_names.json`, `cv_report.txt`. |
| `pipeline.py` | **Feature mátrix** építés az SQLite adatbázisból. 18 feature: ELO diff/abszolút/relatív, győzelmi arány (utolsó 5 meccs), momentum (ELO változás), knockout/stage súly, házigazda státusz, FIFA rang különbség, keretérték diff, formagól diff, tapasztalat diff. API: `build_training_matrix()`, `build_prediction_row()`. |
| `elo.py` | **Saját ELO számítás** az összes meccsre időrendben. K-faktor: barátságos 20, selejtező 25, kontinentális 35-40, VB 60. Knockout szorzó: döntő 1.25×, elődöntő 1.15×. Minden frissítés az `elo_log` táblába kerül. |
| `imported_elo_ratings.py` | **Valós ELO pontszámok** importálása az eloratings.net-ről. Létrehozza a `static_elo` táblát 2026-os, előre kiszámolt értékekkel (pl. Spanyolország 2165, Argentína 2150). |
| `simulation.py` | **Teljes 2026 VB szimuláció**. Csoportkör (12 csoport) → legjobb 8 harmadik kiválasztása → Round of 32 → Round of 16 → Negyeddöntő → Elődöntő → Döntő. Poisson-gólmodell, hosszabbítás és büntetők szimulációja. Eredmény JSON-ba mentve. |
| `feature_names.json` | A 18 feature neve (sorrend a modell számára) |
| `xgb_model.joblib` | A betanított és kalibrált XGBoost modell |
| `cv_report.txt` | TimeSeriesSplit keresztvalidációs jelentés |

### `data/` — Adatbetöltők

| Fájl | Leírás |
|---|---|
| `db.py` | **SQLite adatbázis kapcsolat** (`get_connection()`). WAL mód, foreign keys bekapcsolva, Row factory. `init_db()` létrehozza a táblákat a `database/schema.sql` alapján. |
| `csv_loader.py` | **Kaggle CSV** betöltése (csapat statisztikák tornánként: FIFA rang, keretérték, gólok, VB tapasztalat). UPSERT használ, idempotens. |
| `intl_results_loader.py` | **martj42/international_results** CSV betöltése (1872-től minden válogatott meccs). Csak VB + selejtező meccseket tesz a `match` táblába. `--all` kapcsolóval az összes meccset. |
| `worldcup_json_loader.py` | **openfootball/worldcup.json** betöltése (2002-2026). Meccsek, gólok, büntetők, hosszabbítás. 2026-ra a placeholder csapatokat (pl. "W101") átugorja. |
| `statsbomb_loader.py` | **StatsBomb open data** betöltése (`statsbombpy`). Lineup, xG, lapok feltöltése a 2018-as és 2022-es VB-re. |
| `api_football_loader.py` | **API-Football.com** adatok (keretek, sérülések). Cache-el JSON fájlokba (100 hívás/nap limit). A `player_tournament_stat` táblát tölti. |
| `club_form_loader.py` | **Klub meccsek** betöltése openfootball/football.json-ból (PL, Bundesliga, La Liga, Serie A, Ligue 1). Külön `club_team` és `club_match` táblákba. |
| `audit.py` | **Adatbázis audit eszköz**: rekordszámok, adatminőség, NULL értékek, ELO fedettség ellenőrzése |

### `scripts/` — Napi üzemeltetés

| Fájl | Leírás |
|---|---|
| `update_results.py` | **Eredményfrissítés**: API-Football-ról lekéri az előző nap VB-eredményeit, beírja a `match` táblába, frissíti a `static_elo` értékeket (K=40), és a `daily_predictions` státuszát PENDING → READY_FOR_EVAL-re állítja. Létrehozza a `daily_predictions` és `daily_metrics` táblákat, ha nem léteznek. |
| `evaluator_daily.py` | **Napi kiértékelés**: a READY_FOR_EVAL predikciókat kiértékeli (nyert/vesztett), számolja a napi és kumulált ROI-t, pontosságot. Decimal típus a pénzügyekhez. Eredmény a `daily_metrics` táblába. |
| `daily_predictor.py` | **Napi predikciók**: betölti a betanított modellt, lekéri az Odds API-tól az aktuális szorzókat, kiszámolja a várható értéket (EV) minden mai meccsre, és elmenti a tippeket a `daily_predictions` táblába. |

### `accuracy checker/` — Pontosság-ellenőrző

| Fájl | Leírás |
|---|---|
| `evaluator.py` | A `simulation_predictions.json` fájlban lévő szimulált predikciókat hasonlítja össze a valós odds API adatokkal. Számolja a pontosságot, nettó profitot és ROI-t. |

### `database/`

| Fájl | Leírás |
|---|---|
| `schema.sql` | **Teljes adatbázis séma**. Táblák: `tournament`, `team`, `player`, `team_tournament_stat`, `player_tournament_stat`, `match`, `match_lineup`, `goal_event`, `card_event`, `penalty_shootout`, `elo_log`. View-k: `v_head_to_head`, `v_current_elo`, `v_squad_injuries`. |
| `helper.txt` | Jegyzetek, forrás URL-ek (openfootball, StatsBomb, dbdiagram) |
| `worldcup_database.db` | Az SQLite adatbázis fájl |

---

## Adatfolyam (pipeline)

```
┌─────────────────────┐
│   Adatbetöltők       │
│  (CSV, JSON, API)   │
└────────┬────────────┘
         ↓
┌─────────────────────┐      ┌──────────────────┐
│   Adatbázis          │─────→│   ELO számítás   │
│  (worldcup.db)       │      │  (modell/elo.py)  │
└────────┬────────────┘      └──────────────────┘
         ↓
┌─────────────────────┐
│  Feature mátrix      │
│  (modell/pipeline.py) │
└────────┬────────────┘
         ↓
┌─────────────────────┐      ┌──────────────────┐
│  XGBoost tanítás     │─────→│   xgb_model     │
│  (modell/train.py)   │      │   .joblib        │
└─────────────────────┘      └──────────────────┘
         ↓
    ┌────┴────┐
    ↓         ↓
┌─────────┐ ┌──────────────────┐
│ Napi     │ │ VB Szimuláció    │
│ pipeline │ │ (modell/        │
│ (scripts)│ │ simulation.py)  │
└─────────┘ └──────────────────┘
```

### Napi automatizált pipeline (run_daily.sh)

Cron beállítás (pl. minden reggel 8:00):
```
0 8 * * * /path/to/run_daily.sh >> /path/to/logs/pipeline.log 2>&1
```

Lépések:
1. **`update_results.py`** — Előző nap valós eredményeinek letöltése és mentése
2. **`evaluator_daily.py`** — Tippek kiértékelése, ROI számítás
3. **`daily_predictor.py`** — Mai meccsekre predikciók + odds EV számítás

---

## Alapvető parancsok

### Fejlesztés / egyéni futtatás
```bash
# Projekt gyökér
cd "C:/Users/User/vb predictor"

# Virtuális környezet aktiválása
source .venv/Scripts/activate

# Modell tanítása
python modell/train.py

# ELO újraszámítás
python modell/elo.py
python modell/elo.py --reset   # Törli az elo_log-ot és újrakezdi

# Feature mátrix ellenőrzése
python modell/pipeline.py

# VB szimuláció futtatása
python modell/simulation.py
python modell/simulation.py --seed 123  # Reprodukálható

# Importált ELO ratigns betöltése
python modell/imported_elo_ratings.py

# Adatbázis inicializálása
python data/db.py

# Adatbázis audit
python data/audit.py

# Napi pipeline egyes lépései
python scripts/update_results.py
python scripts/evaluator_daily.py
python scripts/daily_predictor.py

# Teljes napi pipeline
bash run_daily.sh
```

### Adatbetöltők futtatása
```bash
# CSV betöltés
python data/csv_loader.py <fájl.csv>

# Nemzetközi meccseredmények (csak VB + selejtező)
python data/intl_results_loader.py
# Összes meccs (lassabb)
python data/intl_results_loader.py --all
# Adott évtől
python data/intl_results_loader.py --from=2010

# VB JSON (openfootball)
python data/worldcup_json_loader.py
python data/worldcup_json_loader.py 2022  # Csak 2022

# StatsBomb (lineup, xG, lapok)
python data/statsbomb_loader.py
python data/statsbomb_loader.py 2022  # Csak 2022

# API-Football (keretek, sérülések)
python data/api_football_loader.py --key YOUR_KEY
python data/api_football_loader.py --clear-cache  # Cache törlés

# Klub meccsek
python data/club_form_loader.py
python data/club_form_loader.py 2024-25  # adott szezon
```

### Pontosság-ellenőrzés
```bash
python "accuracy checker/evaluator.py"
```

### Adatbázis
```bash
# SQLite konzol (manuális lekérdezésekhez)
sqlite3 database/worldcup_database.db
  .mode column
  .headers on
  SELECT * FROM v_current_elo LIMIT 10;
```

### Git műveletek
```bash
git status
git add .
git commit -m "üzenet"
git push origin main
```

---

## Feature-ök listája (18 db)

A modell által használt feature-ök a `modell/feature_names.json` fájlban:

1. **elo_diff** — home_elo - away_elo a meccs előtt
2. **elo_home** — Hazai csapat abszolút ELO-ja
3. **elo_away** — Vendég csapat abszolút ELO-ja
4. **elo_home_rel** — Hazai ELO / max(ELO-k) (relatív erősség, 0-1)
5. **home_win_rate** — Hazai győzelmi arány utolsó 5 meccs
6. **away_win_rate** — Vendég győzelmi arány utolsó 5 meccs
7. **win_rate_diff** — home_win_rate - away_win_rate
8. **home_momentum** — Hazai ELO változás az utolsó 5 meccsben
9. **away_momentum** — Vendég ELO változás az utolsó 5 meccsben
10. **momentum_diff** — home_momentum - away_momentum
11. **is_knockout** — 1 ha kieséses szakasz
12. **stage_weight** — 1.0 (csoport) .. 1.25 (döntő)
13. **is_host_home** — Hazai csapat házigazda-e
14. **is_host_away** — Vendég csapat házigazda-e
15. **fifa_rank_diff** — home_rank - away_rank
16. **squad_value_diff** — Keretérték különbség (M EUR)
17. **form_goals_diff** — Előző 4 év gólkülönbsége
18. **experience_diff** — VB részvétel különbség

**Célváltozó (outcome):** 0 = vendég győz, 1 = döntetlen, 2 = hazai győz

---

## ELO paraméterek

| Meccs típus | K-faktor |
|---|---|
| FIFA VB | 60 |
| UEFA Euro / Copa América | 40 |
| VB selejtező | 25 |
| UEFA Nations League | 30 |
| Barátságos | 20 |
| Egyéb (default) | 25 |

**Kieséses szorzók**: Döntő 1.25×, Elődöntő 1.15×, Negyeddöntő 1.10×, R16 1.05×

---

## Ismert hibák / Megjegyzések

- **FIFA rang 0 bug** (`élmény.txt`): Ha egy csapatnak nincs 2026-os `team_tournament_stat` bejegyzése, a FIFA rang alapértelmezés 0 lesz, amit a modell "világelsőként" értelmez. Javítás: a `_tts()` függvény most 150-es defaultot ad ha `fifa_rank_pre` hiányzik.
- A pipeline a `static_elo` táblából (importált valós ELO) használja a predikcióhoz, nem az `elo_log`-ból számolt dinamikus ELO-t.
- Az Odds API ingyenes tier-e korlátozott, a napi pipeline cache-el.
