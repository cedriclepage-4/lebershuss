# -*- coding: utf-8 -*-
"""
Optioneel: vult de database met demogegevens om de site uit te proberen.

    python seed_demo.py

Alle demospelers hebben wachtwoord "demo123".
Verwijder daarna gewoon shuss.db om opnieuw met een lege competitie te starten.
"""

import random
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

from database import DB_PATH, init_db, verbind, vrij_spelernummer

NAMEN = ["Jef", "Lore", "Wout", "Amber", "Senne", "Fien", "Milan", "Kato",
         "Robbe", "Noor", "Stan", "Elise"]
TEAMNAMEN = ["De Leverlopers", "Shotgunners", "Duo Penotti", "De Kelderkampioenen",
             "Glazen Garde", "De Natte Nekken", "Borrelbrigade", "Team Tegenwind",
             "De Laatste Ronde", "Promillepatrouille", "De Doordouwers", "Sjotters BV"]


def main():
    init_db()
    # app importeren vóór we zelf schrijven (anders houdt onze transactie de db op slot)
    from app import app, herbereken_alles
    db = verbind(DB_PATH)

    if db.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"] > 0:
        print("De database bevat al spelers — demo wordt niet toegevoegd.")
        return

    demo_hash = generate_password_hash("demo123")
    for i, naam in enumerate(NAMEN):
        # De eerste demospeler is meteen eigenaar, anders raakt niemand in het paneel.
        db.execute("INSERT INTO players (id, name, password_hash, role) "
                   "VALUES (?, ?, ?, ?)",
                   (vrij_spelernummer(db), naam, demo_hash,
                    "eigenaar" if i == 0 else "speler"))
    db.execute("INSERT INTO stat_types (name, unit) VALUES ('Saves', 'stuks')")
    for rang in (("Brons", 0, 900, "#b45309"), ("Zilver", 900, 1000, "#94a3b8"),
                 ("Goud", 1000, 1100, "#f59e0b"), ("Legende", 1100, 3000, "#7c3aed")):
        db.execute("INSERT INTO ranks (name, min_elo, max_elo, color) VALUES (?, ?, ?, ?)", rang)
    db.commit()

    ids = [r["id"] for r in db.execute("SELECT id FROM players")]
    stat_id = db.execute("SELECT id FROM stat_types").fetchone()["id"]

    # Zes actieve teams (elke speler in precies één team).
    random.shuffle(ids)
    team_ids = []
    for i in range(0, len(ids), 2):
        cur = db.execute("INSERT INTO teams (name, description, player1_id, "
                         "player2_id, status) VALUES (?, ?, ?, ?, 'actief')",
                         (TEAMNAMEN[i // 2], "Demoteam met grootse plannen.",
                          ids[i], ids[i + 1]))
        team_ids.append(cur.lastrowid)

    # Vorig seizoen (afgelopen) + huidig seizoen (bezig), elk met speeldagen.
    vandaag = date.today()
    seizoenen = [
        ("Seizoen 1 (demo)", vandaag - timedelta(days=180), vandaag - timedelta(days=60), 3, True),
        ("Seizoen 2 (demo)", vandaag - timedelta(days=30), vandaag + timedelta(days=150), 2, False),
    ]
    for naam, start, einde, aantal_sd, volledig in seizoenen:
        cur = db.execute("INSERT INTO seasons (name, start_date, end_date) "
                         "VALUES (?, ?, ?)", (naam, start.isoformat(), einde.isoformat()))
        seizoen_id = cur.lastrowid
        duur = (einde - start).days
        for n in range(aantal_sd):
            sd_datum = start + timedelta(days=int(duur * (n + 1) / (aantal_sd + 1)))
            cur = db.execute("INSERT INTO matchdays (season_id, title, date) "
                             "VALUES (?, ?, ?)",
                             (seizoen_id, f"Speeldag {n + 1}", sd_datum.isoformat()))
            speeldag_id = cur.lastrowid
            # Per speeldag: 4 wedstrijden tussen willekeurige teams.
            for _ in range(4):
                t1, t2 = random.sample(team_ids, 2)
                moment = datetime.combine(sd_datum, datetime.min.time()).replace(hour=20)
                iso = moment.strftime("%Y-%m-%dT%H:%M")
                speel = volledig or sd_datum <= vandaag
                if speel:
                    winnaar = random.choice((t1, t2))
                    cur = db.execute(
                        "INSERT INTO games (team1_id, team2_id, matchday_id, "
                        "scheduled_at, played_at, status, winner_team_id) "
                        "VALUES (?, ?, ?, ?, ?, 'gespeeld', ?)",
                        (t1, t2, speeldag_id, iso, iso, winnaar))
                    leden = db.execute(
                        "SELECT player1_id, player2_id FROM teams WHERE id IN (?, ?)",
                        (t1, t2)).fetchall()
                    for rij in leden:
                        for pid in (rij["player1_id"], rij["player2_id"]):
                            if random.random() < 0.8:
                                db.execute("INSERT INTO game_stats (game_id, player_id, "
                                           "stat_type_id, value) VALUES (?, ?, ?, ?)",
                                           (cur.lastrowid, pid, stat_id, random.randint(0, 7)))
                else:
                    db.execute("INSERT INTO games (team1_id, team2_id, matchday_id, "
                               "scheduled_at) VALUES (?, ?, ?, ?)",
                               (t1, t2, speeldag_id, iso))
    db.commit()

    with app.app_context():
        herbereken_alles(db)

    print("Demogegevens toegevoegd: 12 spelers (wachtwoord: demo123), 6 teams, "
          "4 rangen, 2 seizoenen met speeldagen en wedstrijden.")
    print(f"Log in als “{NAMEN[0]}” om als eigenaar in het organisatiepaneel te "
          "geraken.")


if __name__ == "__main__":
    main()
