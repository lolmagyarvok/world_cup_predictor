CREATE TABLE tournament (
    id          INTEGER PRIMARY KEY,
    year        INTEGER NOT NULL UNIQUE,
    host_country TEXT   NOT NULL,
    host_continent TEXT NOT NULL    -- UEFA / CONMEBOL / CAF / AFC / CONCACAF / OFC
);
CREATE TABLE team (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,   -- "Brazil", "France", ...
    fifa_code   TEXT    NOT NULL UNIQUE,   -- "BRA", "FRA", ...
    continent   TEXT    NOT NULL           -- UEFA / CONMEBOL / ...
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE player (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    nationality     TEXT    NOT NULL,      -- FIFA kód, pl. "BRA"
    position        TEXT    NOT NULL       -- GK / DEF / MID / FWD
        CHECK(position IN ('GK','DEF','MID','FWD')),
    date_of_birth   TEXT,                  -- ISO 8601: "1987-06-10"
    market_value_eur REAL   DEFAULT 0.0,   -- Transfermarkt, EUR
    club            TEXT                   -- aktuális klub neve
);
CREATE UNIQUE INDEX idx_player_name_nat
    ON player(name, nationality);
CREATE TABLE team_tournament_stat (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id                     INTEGER NOT NULL REFERENCES team(id),
    tournament_id               INTEGER NOT NULL REFERENCES tournament(id),

    -- FIFA adatok a torna előtt
    is_host                     INTEGER NOT NULL DEFAULT 0 CHECK(is_host IN (0,1)),
    fifa_rank_pre               INTEGER,
    fifa_points_pre             REAL,
    squad_total_value_eur       REAL    DEFAULT 0.0,
    squad_avg_age               REAL,

    -- Utolsó 4 év forma (CSV-ből)
    goals_scored_last_4y        INTEGER DEFAULT 0,
    goals_received_last_4y      INTEGER DEFAULT 0,
    wins_last_4y                INTEGER DEFAULT 0,
    draws_last_4y               INTEGER DEFAULT 0,
    losses_last_4y              INTEGER DEFAULT 0,

    -- Historikus VB tapasztalat
    world_cup_titles_before         INTEGER DEFAULT 0,
    world_cup_participations_before INTEGER DEFAULT 0,
    groups_passed_before            INTEGER DEFAULT 0,
    round16_before                  INTEGER DEFAULT 0,
    quarterfinals_before            INTEGER DEFAULT 0,
    semifinals_before               INTEGER DEFAULT 0,
    finals_before                   INTEGER DEFAULT 0,

    -- ELO (saját számítású, meccs előtt/után)
    elo_pre                     REAL    DEFAULT 1500.0,
    elo_post                    REAL,   -- NULL amíg a torna véget nem ér

    -- Torna eredmény (ground truth a modell tanításához)
    winner                      INTEGER DEFAULT 0 CHECK(winner IN (0,1)),
    finalist                    INTEGER DEFAULT 0 CHECK(finalist IN (0,1)),
    semi_finalist               INTEGER DEFAULT 0 CHECK(semi_finalist IN (0,1)),
    quarter_finalist            INTEGER DEFAULT 0 CHECK(quarter_finalist IN (0,1)),

    UNIQUE(team_id, tournament_id)
);
CREATE TABLE player_tournament_stat (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id           INTEGER NOT NULL REFERENCES player(id),
    tournament_id       INTEGER NOT NULL REFERENCES tournament(id),
    team_id             INTEGER NOT NULL REFERENCES team(id),

    -- Aktuális állapot (napi frissítés a VB alatt)
    is_injured          INTEGER NOT NULL DEFAULT 0 CHECK(is_injured IN (0,1)),
    is_key_player       INTEGER NOT NULL DEFAULT 0 CHECK(is_key_player IN (0,1)),
    injury_severity     TEXT    CHECK(injury_severity IN ('minor','moderate','severe',NULL)),
    return_date_est     TEXT,   -- ISO 8601, becsült visszatérés

    -- Torna előtti forma
    form_rating         REAL,   -- SofaScore rating átlag (utolsó 5 meccs)
    caps                INTEGER DEFAULT 0,
    goals               INTEGER DEFAULT 0,
    assists             INTEGER DEFAULT 0,
    pass_accuracy       REAL,   -- százalék, 0.0-100.0
    rating_sofascore    REAL,   -- 0.0-10.0
    minutes_played      INTEGER DEFAULT 0,

    UNIQUE(player_id, tournament_id)
);
CREATE TABLE match (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id   INTEGER NOT NULL REFERENCES tournament(id),
    home_team_id    INTEGER NOT NULL REFERENCES team(id),
    away_team_id    INTEGER NOT NULL REFERENCES team(id),

    stage           TEXT    NOT NULL,  -- "Group A", "Round of 16", "Final", ...
    match_date      TEXT    NOT NULL,  -- ISO 8601: "2022-12-18"
    match_time      TEXT,              -- "18:00" UTC
    venue           TEXT,
    city            TEXT,

    -- Rendes játékidő
    home_score      INTEGER,
    away_score      INTEGER,

    -- Hosszabbítás (NULL ha nem volt)
    home_score_aet  INTEGER,
    away_score_aet  INTEGER,

    -- Büntetők (NULL ha nem volt)
    home_pens       INTEGER,
    away_pens       INTEGER,

    -- Feature (meccs előtt számolva, ELO különbség)
    elo_diff_pre    REAL,  -- home_elo - away_elo a meccs előtt

    CHECK(home_team_id != away_team_id)
);
CREATE INDEX idx_match_tournament ON match(tournament_id);
CREATE INDEX idx_match_home       ON match(home_team_id);
CREATE INDEX idx_match_away       ON match(away_team_id);
CREATE TABLE match_lineup (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    player_id   INTEGER NOT NULL REFERENCES player(id),
    team_id     INTEGER NOT NULL REFERENCES team(id),

    is_starter  INTEGER NOT NULL DEFAULT 1 CHECK(is_starter IN (0,1)),
    minute_in   INTEGER,   -- NULL = kezdőtől; csere esetén: mikor jött be
    minute_out  INTEGER,   -- NULL = végig játszott

    UNIQUE(match_id, player_id)
);
CREATE INDEX idx_lineup_match ON match_lineup(match_id);
CREATE TABLE goal_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    player_id   INTEGER         REFERENCES player(id),  -- NULL = ismeretlen
    team_id     INTEGER NOT NULL REFERENCES team(id),

    minute      INTEGER NOT NULL,
    is_penalty  INTEGER NOT NULL DEFAULT 0 CHECK(is_penalty IN (0,1)),
    is_own_goal INTEGER NOT NULL DEFAULT 0 CHECK(is_own_goal IN (0,1))
);
CREATE INDEX idx_goal_match ON goal_event(match_id);
CREATE TABLE card_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    player_id   INTEGER         REFERENCES player(id),
    team_id     INTEGER NOT NULL REFERENCES team(id),

    minute      INTEGER NOT NULL,
    card_type   TEXT    NOT NULL CHECK(card_type IN ('yellow','red','yellow_red'))
);
CREATE TABLE penalty_shootout (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    player_id   INTEGER         REFERENCES player(id),
    team_id     INTEGER NOT NULL REFERENCES team(id),

    order_num   INTEGER NOT NULL,   -- rúgás sorrendje (1-től)
    scored      INTEGER NOT NULL CHECK(scored IN (0,1))
);
CREATE INDEX idx_pen_match ON penalty_shootout(match_id);
CREATE TABLE elo_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     INTEGER NOT NULL REFERENCES team(id),
    match_id    INTEGER NOT NULL REFERENCES match(id),
    elo_before  REAL    NOT NULL,
    elo_after   REAL    NOT NULL,
    k_factor    REAL    NOT NULL DEFAULT 20.0,
    logged_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE VIEW v_head_to_head AS
SELECT
    m.home_team_id  AS team_a_id,
    m.away_team_id  AS team_b_id,
    COUNT(*)        AS matches_played,
    SUM(CASE WHEN m.home_score > m.away_score THEN 1 ELSE 0 END) AS team_a_wins,
    SUM(CASE WHEN m.home_score = m.away_score THEN 1 ELSE 0 END) AS draws,
    SUM(CASE WHEN m.home_score < m.away_score THEN 1 ELSE 0 END) AS team_b_wins,
    AVG(m.home_score) AS avg_goals_a,
    AVG(m.away_score) AS avg_goals_b
FROM match m
GROUP BY m.home_team_id, m.away_team_id
/* v_head_to_head(team_a_id,team_b_id,matches_played,team_a_wins,draws,team_b_wins,avg_goals_a,avg_goals_b) */;
CREATE VIEW v_current_elo AS
SELECT
    el.team_id,
    t.name,
    el.elo_after AS current_elo,
    el.logged_at
FROM elo_log el
JOIN team t ON t.id = el.team_id
WHERE el.id = (
    SELECT id FROM elo_log
    WHERE team_id = el.team_id
    ORDER BY logged_at DESC
    LIMIT 1
)
/* v_current_elo(team_id,name,current_elo,logged_at) */;
CREATE VIEW v_squad_injuries AS
SELECT
    pts.team_id,
    t.name AS team_name,
    pts.tournament_id,
    COUNT(*) FILTER (WHERE pts.is_injured = 1)                    AS injured_count,
    COUNT(*) FILTER (WHERE pts.is_injured = 1 AND pts.is_key_player = 1) AS key_injured_count,
    SUM(p.market_value_eur) FILTER (WHERE pts.is_injured = 1)    AS injured_value_eur
FROM player_tournament_stat pts
JOIN player p  ON p.id  = pts.player_id
JOIN team   t  ON t.id  = pts.team_id
GROUP BY pts.team_id, pts.tournament_id
/* v_squad_injuries(team_id,team_name,tournament_id,injured_count,key_injured_count,injured_value_eur) */;
