# -*- coding: utf-8 -*-
"""Databaselaag van Lebershuss Tonzent (SQLite) — schema v5 (liga + toernooien).

Nieuw in v5
-----------
* Spelers en teams hebben een **permanente ELO** (`players.elo`, `teams.elo`):
  die telt alles mee, zowel ligawedstrijden als toernooiwedstrijden.
* Daarnaast heeft elk **seizoen zijn eigen ELO** (`season_ratings`): bij de start
  van een seizoen begint iedereen opnieuw op 1000. Enkel ligawedstrijden tellen
  mee voor de seizoens-ELO.
* De site is opgesplitst in twee delen: **liga** (seizoenen, speeldagen) en
  **toernooi** (bracketfase + knockout), met dezelfde spelers en teams.
* Er is geen algemeen adminwachtwoord meer: elke speler heeft een **rol**
  (`players.role`) — gewone speler, organisator of eigenaar.
"""

import os
import random
import shutil
import sqlite3
from datetime import datetime

# Spelersnummers zijn 4 cijfers en worden willekeurig toegekend, zodat je aan het
# nummer niet kan zien wie zich als eerste registreerde.
SPELER_MIN, SPELER_MAX = 1000, 9999

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "shuss.db")

# Rollen: 'speler' (gewoon), 'admin' (organisator) en 'eigenaar' (hoofdorganisator,
# er is er altijd hoogstens één). Er is geen algemeen adminwachtwoord meer.
ROL_SPELER, ROL_ADMIN, ROL_EIGENAAR = "speler", "admin", "eigenaar"

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,          -- echte naam, wordt niet gewijzigd
    nickname      TEXT NOT NULL DEFAULT '',      -- vrij aanpasbaar
    password_hash TEXT,
    role          TEXT NOT NULL DEFAULT 'speler',
    elo           REAL NOT NULL DEFAULT 1000,   -- permanente ELO (liga + toernooi)
    active        INTEGER NOT NULL DEFAULT 1,
    avatar        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Teams worden door spelers zelf gemaakt: speler 1 (maker) nodigt speler 2 uit.
-- Pas na aanvaarden wordt het team 'actief' en kan het wedstrijden spelen.
CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    player1_id  INTEGER NOT NULL REFERENCES players(id),
    player2_id  INTEGER NOT NULL REFERENCES players(id),
    status      TEXT NOT NULL DEFAULT 'in_afwachting'
                CHECK (status IN ('in_afwachting', 'actief')),
    avatar      TEXT,
    elo         REAL NOT NULL DEFAULT 1000,     -- permanente ELO (liga + toernooi)
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------- liga --

CREATE TABLE IF NOT EXISTS seasons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS matchdays (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    title     TEXT NOT NULL,
    date      TEXT NOT NULL
);

-- ELO binnen één seizoen: iedereen start er op 1000.
CREATE TABLE IF NOT EXISTS season_ratings (
    season_id   INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('speler', 'team')),
    entity_id   INTEGER NOT NULL,
    elo         REAL NOT NULL,
    gespeeld    INTEGER NOT NULL DEFAULT 0,
    winst       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season_id, entity_type, entity_id)
);

-- ----------------------------------------------------------- toernooi --

CREATE TABLE IF NOT EXISTS tournaments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    description    TEXT NOT NULL DEFAULT '',
    date           TEXT NOT NULL,                  -- speeldatum (JJJJ-MM-DD)
    start_tijd     TEXT NOT NULL DEFAULT '19:00',
    bracket_ronden INTEGER NOT NULL DEFAULT 4,     -- games per team in de bracketfase
    ko_teams       INTEGER NOT NULL DEFAULT 8,     -- hoeveel teams naar de knockout
    potten         INTEGER NOT NULL DEFAULT 4,
    slot_minuten   INTEGER NOT NULL DEFAULT 20,
    status         TEXT NOT NULL DEFAULT 'opzet'
                   CHECK (status IN ('opzet', 'bracket', 'knockout', 'afgelopen')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS tournament_locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    UNIQUE (tournament_id, name)
);

CREATE TABLE IF NOT EXISTS tournament_teams (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    team_id       INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    pot           INTEGER,
    seed          INTEGER,
    start_elo     REAL,
    UNIQUE (tournament_id, team_id)
);

-- ---------------------------------------------------------- wedstrijden --

-- Eén tabel voor álle wedstrijden: liga én toernooi. Zo delen beide delen van
-- de site dezelfde spelers, teams, meldingen en statistieken.
CREATE TABLE IF NOT EXISTS games (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    team1_id         INTEGER REFERENCES teams(id),
    team2_id         INTEGER REFERENCES teams(id),
    matchday_id      INTEGER REFERENCES matchdays(id) ON DELETE SET NULL,
    tournament_id    INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
    fase             TEXT NOT NULL DEFAULT 'liga'
                     CHECK (fase IN ('liga', 'bracket', 'shootout', 'knockout')),
    ronde            INTEGER,   -- bracket: rondenummer; knockout: aantal teams (16, 8, 4, 2)
    positie          INTEGER,   -- plaats binnen de knockoutronde (bracketweergave)
    volgende_game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
    volgende_slot    INTEGER,   -- 1 of 2: in welk vak de winnaar terechtkomt
    location_id      INTEGER REFERENCES tournament_locations(id) ON DELETE SET NULL,
    scheduled_at     TEXT NOT NULL,
    played_at        TEXT,
    status           TEXT NOT NULL DEFAULT 'gepland'
                     CHECK (status IN ('gepland', 'gespeeld')),
    winner_team_id   INTEGER REFERENCES teams(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Types statistieken die organisatoren zelf kunnen aanmaken (bv. "saves").
CREATE TABLE IF NOT EXISTS stat_types (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    unit       TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Waarden van die statistieken, per wedstrijd en per speler (geen invloed op ELO).
CREATE TABLE IF NOT EXISTS game_stats (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id    INTEGER NOT NULL REFERENCES players(id),
    stat_type_id INTEGER NOT NULL REFERENCES stat_types(id) ON DELETE CASCADE,
    value        REAL NOT NULL,
    UNIQUE (game_id, player_id, stat_type_id)
);

-- Meldingen van spelers over de uitslag (één per team per wedstrijd).
CREATE TABLE IF NOT EXISTS game_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    team_id        INTEGER NOT NULL REFERENCES teams(id),
    player_id      INTEGER NOT NULL REFERENCES players(id),
    winner_team_id INTEGER NOT NULL REFERENCES teams(id),
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (game_id, team_id)
);

-- Historiek van elke ELO-wijziging. scope = 'permanent' (alles) of 'seizoen'.
CREATE TABLE IF NOT EXISTS rating_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('speler', 'team')),
    entity_id   INTEGER NOT NULL,
    elo_voor    REAL NOT NULL,
    elo_na      REAL NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'permanent'
                CHECK (scope IN ('permanent', 'seizoen')),
    season_id   INTEGER
);

-- Zelf te definiëren rangen, bv. "Zilver" van 900 tot 1000 ELO.
CREATE TABLE IF NOT EXISTS ranks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    min_elo REAL NOT NULL,
    max_elo REAL NOT NULL,
    color   TEXT NOT NULL DEFAULT '#64748b'
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Logboek van opgeëiste accounts. De organisator maakt spelers aan zonder
-- wachtwoord; de speler zelf kiest er later één ("claimen"). Elke claim wordt
-- hier bijgehouden, zodat een vergissing zichtbaar is en teruggedraaid kan
-- worden. De naam wordt mee bewaard: ook na het verwijderen van een speler
-- blijft leesbaar wat er gebeurd is.
CREATE TABLE IF NOT EXISTS claim_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id  INTEGER,
    naam       TEXT NOT NULL,
    soort      TEXT NOT NULL DEFAULT 'claim'
               CHECK (soort IN ('claim', 'vrijgegeven')),
    ip         TEXT,
    moment     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_games_toernooi ON games(tournament_id);
CREATE INDEX IF NOT EXISTS idx_games_speeldag ON games(matchday_id);
CREATE INDEX IF NOT EXISTS idx_rh_game ON rating_history(game_id, scope);
"""

STANDAARD_INSTELLINGEN = {
    "k_speler": "32",
    "k_team": "32",
    # Staat het leaguegedeelte (klassement, wedstrijden, seizoenen) open voor de
    # spelers? Uit betekent: enkel het toernooigedeelte. Organisatoren zien de
    # leaguepagina's dan nog wel. Om te zetten via Organisatie → Instellingen.
    "league_actief": "0",
    # Mogen spelers een account zonder wachtwoord opeisen? Zet dit enkel open
    # terwijl je erbij bent (bv. tijdens het toernooi zelf): wie het venster
    # openzet, vertrouwt erop dat de mensen in de zaal elkaars account niet
    # nemen. Elke claim komt in claim_log te staan.
    "claim_open": "0",
}

# Nieuwe games-tabel (voor de migratie van een v4-database).
GAMES_V5 = """
CREATE TABLE games_nieuw (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    team1_id         INTEGER REFERENCES teams(id),
    team2_id         INTEGER REFERENCES teams(id),
    matchday_id      INTEGER REFERENCES matchdays(id) ON DELETE SET NULL,
    tournament_id    INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
    fase             TEXT NOT NULL DEFAULT 'liga'
                     CHECK (fase IN ('liga', 'bracket', 'shootout', 'knockout')),
    ronde            INTEGER,
    positie          INTEGER,
    volgende_game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
    volgende_slot    INTEGER,
    location_id      INTEGER REFERENCES tournament_locations(id) ON DELETE SET NULL,
    scheduled_at     TEXT NOT NULL,
    played_at        TEXT,
    status           TEXT NOT NULL DEFAULT 'gepland'
                     CHECK (status IN ('gepland', 'gespeeld')),
    winner_team_id   INTEGER REFERENCES teams(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def verbind(pad: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(pad)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _kolommen(conn, tabel):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({tabel})")}


def _is_oud_schema(pad: str) -> bool:
    """Waar als er een database van vóór v4 staat (teams zonder naamkolom)."""
    try:
        conn = sqlite3.connect(pad)
        kolommen = {r[1] for r in conn.execute("PRAGMA table_info(teams)")}
        conn.close()
        return bool(kolommen) and "name" not in kolommen
    except sqlite3.Error:
        return False


def _backup(pad: str) -> str:
    kopie = os.path.join(os.path.dirname(pad),
                         f"shuss_backup_{datetime.now():%Y%m%d_%H%M%S}.db")
    shutil.copy2(pad, kopie)
    return kopie


def _migreer_rollen(conn):
    """Voeg de rolkolom toe aan een bestaande spelerstabel.

    Bestaande spelers worden gewone spelers: wie de organisatie doet, wordt
    daarna aangeduid met `python app.py --eigenaar <naam of nummer>`. Zo kan
    niemand zomaar organisator worden door een oude database te gebruiken.
    """
    kolommen = _kolommen(conn, "players")
    if kolommen and "role" not in kolommen:
        conn.execute("ALTER TABLE players ADD COLUMN role TEXT NOT NULL "
                     "DEFAULT 'speler'")
        conn.commit()


def _migreer(conn, pad):
    """Werk een bestaande v4-database bij naar v5 (met back-up vooraf)."""
    games_kolommen = _kolommen(conn, "games")
    rh_kolommen = _kolommen(conn, "rating_history")
    nodig = ("tournament_id" not in games_kolommen and games_kolommen) or \
            ("scope" not in rh_kolommen and rh_kolommen)
    if not nodig:
        return

    if os.path.exists(pad):
        print(f"Database bijwerken naar v5 — back-up: {os.path.basename(_backup(pad))}")

    if rh_kolommen and "scope" not in rh_kolommen:
        conn.execute("ALTER TABLE rating_history ADD COLUMN scope TEXT NOT NULL "
                     "DEFAULT 'permanent'")
        conn.execute("ALTER TABLE rating_history ADD COLUMN season_id INTEGER")

    if games_kolommen and "tournament_id" not in games_kolommen:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(GAMES_V5)
        conn.execute("""
            INSERT INTO games_nieuw (id, team1_id, team2_id, matchday_id, fase,
                                     scheduled_at, played_at, status, winner_team_id,
                                     created_at)
            SELECT id, team1_id, team2_id, matchday_id, 'liga',
                   scheduled_at, played_at, status, winner_team_id, created_at
            FROM games
        """)
        conn.execute("DROP TABLE games")
        conn.execute("ALTER TABLE games_nieuw RENAME TO games")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


# Alle plaatsen waar een speler-id in voorkomt (buiten players zelf).
_SPELER_VERWIJZINGEN = [
    ("teams", "player1_id", None),
    ("teams", "player2_id", None),
    ("game_stats", "player_id", None),
    ("game_reports", "player_id", None),
    ("rating_history", "entity_id", "entity_type = 'speler'"),
    ("season_ratings", "entity_id", "entity_type = 'speler'"),
]


def vrij_spelernummer(conn) -> int:
    """Een ongebruikt willekeurig spelersnummer van 4 cijfers."""
    bezet = {r[0] for r in conn.execute("SELECT id FROM players")}
    vrij = [n for n in range(SPELER_MIN, SPELER_MAX + 1) if n not in bezet]
    if not vrij:
        raise RuntimeError("Alle spelersnummers zijn op.")
    return random.choice(vrij)


def _hernummer_spelers(conn, pad):
    """Geef bestaande spelers een willekeurig nummer van 4 cijfers.

    De oude, oplopende id's verraadden de volgorde van registratie. Alle
    verwijzingen (teams, statistieken, meldingen, ratinghistoriek) verhuizen mee.
    Er wordt in twee stappen gewerkt — eerst alles naar een hoog tijdelijk nummer —
    zodat een nieuw nummer nooit botst met een bestaand nummer.
    """
    ids = [r[0] for r in conn.execute("SELECT id FROM players ORDER BY id")]
    if not ids or min(ids) >= SPELER_MIN:
        return
    if os.path.exists(pad):
        print(f"Spelersnummers vernieuwen — back-up: {os.path.basename(_backup(pad))}")

    beschikbaar = [n for n in range(SPELER_MIN, SPELER_MAX + 1) if n not in set(ids)]
    random.shuffle(beschikbaar)
    nieuw = {oud: beschikbaar[i] for i, oud in enumerate(ids)}
    tijdelijk = 1_000_000

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f"UPDATE players SET id = id + {tijdelijk}")
    for tabel, kolom, extra in _SPELER_VERWIJZINGEN:
        waar = f" WHERE {extra}" if extra else ""
        conn.execute(f"UPDATE {tabel} SET {kolom} = {kolom} + {tijdelijk}{waar}")

    for oud, nw in nieuw.items():
        conn.execute("UPDATE players SET id = ? WHERE id = ?", (nw, oud + tijdelijk))
        for tabel, kolom, extra in _SPELER_VERWIJZINGEN:
            waar = f" AND {extra}" if extra else ""
            conn.execute(f"UPDATE {tabel} SET {kolom} = ? WHERE {kolom} = ?{waar}",
                         (nw, oud + tijdelijk))

    conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'players'",
                 (max(nieuw.values()),))
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    print(f"{len(nieuw)} speler(s) kregen een nieuw nummer.")


def init_db(pad: str = DB_PATH) -> None:
    """Maak alle tabellen en standaardinstellingen aan (idempotent).

    Een database van vóór v4 wordt opzijgezet als back-up en vervangen door een
    verse database. Een v4-database wordt ter plaatse bijgewerkt naar v5:
    de bestaande ELO wordt de permanente ELO en alle wedstrijden krijgen de
    fase 'liga'.
    """
    if os.path.exists(pad) and _is_oud_schema(pad):
        backup = os.path.join(os.path.dirname(pad),
                              f"shuss_oud_{datetime.now():%Y%m%d_%H%M%S}.db")
        os.rename(pad, backup)
        print(f"Oude database opzijgezet als {os.path.basename(backup)}; "
              "er is een nieuwe, lege database aangemaakt.")

    conn = verbind(pad)
    _migreer(conn, pad)
    _migreer_rollen(conn)
    conn.executescript(SCHEMA)
    _hernummer_spelers(conn, pad)
    for sleutel, waarde in STANDAARD_INSTELLINGEN.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                     (sleutel, waarde))
    # Het oude algemene adminwachtwoord is vervangen door rollen per speler.
    conn.execute("DELETE FROM settings WHERE key = 'admin_hash'")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database klaar: {DB_PATH}")
