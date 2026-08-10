# World Cup Predictor 2026
Machine learning system that predicts FIFA World Cup 2026 match outcomes 
and generates daily betting recommendations based on expected value (EV).

Built on a custom Elo rating system, an XGBoost classifier (18 features), 
and a full tournament simulation (group stage → knockout → final).

**Key features**
- Multi-source data ingestion(all international matches since 2002, StatsBomb, API-Football)
- Custom Elo rating calculation for every match
- XGBoost classifiers(18 features: Elo, form, tournament context, FIFA ranking, experience)
- Automated daily pipeline: result update -> evaluation -> new prediction
- Full 2026 World Cup simulation: group stage -> knockout rounds -> final

## Architecture
```
Data loaders(CSV, JSON, API)
            |
            v
Database(worldcup_database.db)
            |
            v
Elo calculation(modell/elo.py) 
            |
            v
XGBoost Training(modell/train.py)
            |
    |-------|--------|
    v                v   
    Daily           Full 
    predictions     World Cup
    (pipeline)      simulation
```

## Getting started

### Requirements
requirements.txt


### Setup

```bash
# clone the project and enter the directory

cd world_cup_predictor

# create and activate a new virtual environement

python -m venv .venv
source .venv/Scripts/activate

# install the dependencies
pip install -r requirements.txt

# configure you API keys (API-Football, Odds API)

cp .env.example .env #then fill in you keys

# initialize the database

python data/db.py
```


## Usage
 
### Development commands
 
```bash
# train the model
python modell/train.py
 
# recalculate Elo ratings
python modell/elo.py
python modell/elo.py --reset          # wipe elo_log and start over
 
# run the World Cup simulation
python modell/simulation.py
python modell/simulation.py --seed 123   # reproducible run
 
# check database health
python data/audit.py
```
 
### Daily automated pipeline
 
```bash
bash run_daily.sh
```
 
Runs, in order:
1. `scripts/update_results.py` — fetch and store yesterday's real results
2. `scripts/evaluator_daily.py` — evaluate pending tips, compute ROI
3. `scripts/daily_predictor.py` — generate today's predictions with betting EV
For cron scheduling, backfilling missed days, and individual data loaders, see [`CLAUDE.md`](./CLAUDE.md#alapvető-parancsok).
 
---
 
## Project structure
 
| Path | Contents |
|---|---|
| `modell/` | Elo rating, feature engineering, XGBoost training, tournament simulation |
| `data/` | Data loaders and API clients (CSV, StatsBomb, API-Football, etc.) |
| `database/` | SQLite schema (`schema.sql`) and the database file |
| `scripts/` | Daily pipeline: result updates, evaluation, predictions |
| `accuracy checker/` | Offline accuracy/ROI checker for simulation output |
 
---
 
## Known limitations
 
- Predictions use the imported static Elo table (`static_elo`), not the dynamically computed `elo_log`.
- The free Odds API tier is rate-limited; the daily pipeline caches responses.
- See `TODO.txt` for open items.