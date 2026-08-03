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

