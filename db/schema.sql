-- NFL 2.0 schema. See nfl_handicapping_framework.md for the design rationale.

CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,      -- nflverse team abbr, e.g. 'BUF'
    name TEXT,
    conference TEXT,
    division TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,    -- gsis_id when known, else pfr_id
    gsis_id TEXT,
    pfr_id TEXT,
    name TEXT NOT NULL,
    position TEXT,
    team_abbr TEXT
);
CREATE INDEX IF NOT EXISTS idx_players_pfr ON players(pfr_id);
CREATE INDEX IF NOT EXISTS idx_players_gsis ON players(gsis_id);

CREATE TABLE IF NOT EXISTS player_game_stats (
    player_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    team_abbr TEXT,
    opponent_abbr TEXT,
    snap_pct REAL,          -- max(offense_pct, defense_pct, st_pct)
    offense_pct REAL,
    defense_pct REAL,
    st_pct REAL,
    stat_json TEXT,         -- raw player_stats.csv fields for that game
    PRIMARY KEY (player_id, season, week)
);
CREATE INDEX IF NOT EXISTS idx_pgs_team_season_week ON player_game_stats(team_abbr, season, week);

CREATE TABLE IF NOT EXISTS player_grades (
    player_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    grade REAL,             -- 0-100
    source TEXT,            -- 'individual' or 'team_unit_proxy'
    grade_components_json TEXT,
    PRIMARY KEY (player_id, season, week)
);

CREATE TABLE IF NOT EXISTS position_group_scores (
    team_abbr TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    position_group TEXT NOT NULL,   -- QB, RB, WR, TE, OL, DEF, ST
    score REAL,
    snap_weighted_n REAL,
    PRIMARY KEY (team_abbr, season, week, position_group)
);

CREATE TABLE IF NOT EXISTS team_scores (
    team_abbr TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    offense_score REAL,
    defense_score REAL,
    st_score REAL,
    overall_score REAL,
    rolling_window_games INTEGER,
    PRIMARY KEY (team_abbr, season, week)
);

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,       -- nflverse game_id, e.g. '2025_02_BUF_NYJ'
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    game_type TEXT,
    kickoff_utc TEXT,
    home_abbr TEXT NOT NULL,
    away_abbr TEXT NOT NULL,
    roof_type TEXT,
    is_divisional INTEGER,
    rest_days_home INTEGER,
    rest_days_away INTEGER,
    closing_spread REAL,    -- home-team spread_line from nfldata games.csv
    closing_total REAL,
    referee TEXT,
    home_score INTEGER,
    away_score INTEGER
);
CREATE INDEX IF NOT EXISTS idx_games_season_week ON games(season, week);

CREATE TABLE IF NOT EXISTS weather (
    game_id TEXT PRIMARY KEY,
    temp_f REAL,
    wind_mph REAL,
    precip_type TEXT,
    precip_prob REAL,
    alert_text TEXT,
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS injuries (
    team_abbr TEXT NOT NULL,
    player_name TEXT NOT NULL,
    position TEXT,
    status TEXT,         -- Out | Doubtful | Questionable | IR | etc, as ESPN prints it
    comment TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_abbr, player_name)
);

CREATE TABLE IF NOT EXISTS officiating (
    game_id TEXT PRIMARY KEY,
    referee_name TEXT,
    crew_home_ats_pct REAL,
    crew_over_pct REAL,
    crew_games INTEGER,
    assigned_at TEXT
);

CREATE TABLE IF NOT EXISTS line_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    spread REAL,     -- home-team spread, same positive-means-home-favored convention as games.closing_spread
    total REAL,
    UNIQUE(game_id, checked_at)
);

CREATE TABLE IF NOT EXISTS splits (
    game_id TEXT NOT NULL,
    bet_type TEXT NOT NULL,     -- spread | total | moneyline
    side TEXT NOT NULL,         -- home | away | over | under
    bets_pct REAL,
    handle_pct REAL,
    checked_at TEXT,
    PRIMARY KEY (game_id, bet_type, side)
);

CREATE TABLE IF NOT EXISTS projections (
    game_id TEXT PRIMARY KEY,
    base_score_diff REAL,
    hfa_adj REAL,
    weather_adj REAL,
    rest_adj REAL,
    rivalry_adj REAL,
    ref_adj REAL,
    injury_adj REAL,          -- listed players priced at replacement level, in spread points
    pre_shrink_spread REAL,   -- 100% model, before blending toward market
    final_spread REAL,        -- shrunk toward closing/current market line
    final_total REAL,
    home_win_prob REAL,
    confidence_score REAL,
    generated_at TEXT
);

CREATE TABLE IF NOT EXISTS best_bets (
    game_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    bet_type TEXT NOT NULL,     -- spread | total
    pick TEXT NOT NULL,
    confidence_score REAL,
    reasoning_text TEXT,
    PRIMARY KEY (game_id, bet_type)
);

CREATE TABLE IF NOT EXISTS backtest_log (
    game_id TEXT PRIMARY KEY,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    predicted_spread REAL,
    closing_spread REAL,
    error_spread REAL,
    predicted_total REAL,
    closing_total REAL,
    error_total REAL,
    ats_result TEXT,     -- win | loss | push | NULL (game not final)
    ou_result TEXT,       -- over | under | push | NULL
    adjustments_fired_json TEXT  -- which adjustments were non-zero, for per-factor backtest breakdown
);
