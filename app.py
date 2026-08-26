# -*- coding: utf-8 -*-
"""
Leberschuss Tonzent — webapplicatie voor de schusscompetitie van jeugdhuis Tonzent.

Starten:
    pip install flask
    python app.py
Daarna surfen naar http://localhost:5000

Standaard beheerderswachtwoord: "leberschuss" (wijzig meteen via Organisatie > Instellingen!)
Spelers maken zelf een account aan, vormen zelf teams (met uitnodigingen) en
melden zelf hun uitslagen; organisatoren beheren seizoenen, speeldagen en
wedstrijden.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime
from functools import wraps
from itertools import groupby

# ---------------------------------------------------------------- tijdzone --
# Hostingservers staan bijna altijd op UTC; dan zouden alle uren twee uur
# verkeerd staan (zowel in Python als in de datetime('now','localtime') van
# SQLite). Daarom zetten we de tijdzone van het proces zelf goed, vóór er ook
# maar iets met tijd gebeurt. Elders in de wereld? Zet TIJDZONE in de omgeving.
TIJDZONE = os.environ.get("TIJDZONE", "Europe/Brussels")
os.environ["TZ"] = TIJDZONE
if hasattr(time, "tzset"):          # Linux en macOS; op Windows niet nodig
    time.tzset()

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

import tournament as toernooi_motor
from database import (DB_PATH, ROL_ADMIN, ROL_EIGENAAR, ROL_SPELER, init_db,
                      verbind, vrij_spelernummer)
from elo import (START_ELO, fase_factor, fase_omschrijving, proces_wedstrijd)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TEMPLATE_MAP = os.path.join(BASE_DIR, "templates")
STATIC_MAP = os.path.join(BASE_DIR, "static")
if not os.path.isdir(TEMPLATE_MAP):
    TEMPLATE_MAP = BASE_DIR
if not os.path.isdir(STATIC_MAP):
    STATIC_MAP = BASE_DIR
UPLOAD_MAP = os.path.join(STATIC_MAP, "uploads")

BACKUP_MAP = os.path.join(BASE_DIR, "backups")

app = Flask(__name__, template_folder=TEMPLATE_MAP, static_folder=None)
# Ruim genoeg voor een databaseback-up; profielfoto's worden apart beperkt tot
# 3 MB in bewaar_avatar(), zodat niemand een reuzenfoto kan uploaden.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
MAX_AVATAR = 3 * 1024 * 1024

_SLEUTELBESTAND = os.path.join(BASE_DIR, ".secret_key")
if os.path.exists(_SLEUTELBESTAND):
    with open(_SLEUTELBESTAND) as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(_SLEUTELBESTAND, "w") as f:
        f.write(app.secret_key)

# ------------------------------------------------------------- achter nginx --
# Draait de site op een echte server, dan zit er meestal een webserver vóór die
# de https-verbinding afhandelt. Twee dingen moeten dan kloppen:
#
#   ACHTER_PROXY=1  → vertrouw de X-Forwarded-headers van die webserver, zodat
#                     we het échte IP-adres van de bezoeker zien (voor het
#                     claimlogboek) en weten dat de verbinding https is.
#   HTTPS=1         → de sessiecookie mag enkel over https verstuurd worden.
#
# Zet ACHTER_PROXY nooit aan zonder zo'n webserver ervoor: dan kan iedereen
# zelf een X-Forwarded-For meesturen en zijn de IP's in het logboek verzonnen.
app.config["SESSION_COOKIE_HTTPONLY"] = True     # niet leesbaar voor javascript
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"    # niet meesturen vanaf andere sites
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("HTTPS") == "1"

if os.environ.get("ACHTER_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
           "augustus", "september", "oktober", "november", "december"]

_TOEGELATEN_STATIC = {"style.css", "app.js", "logo.svg", "logo.png",
                      "manifest.webmanifest", "icon-192.png", "icon-512.png",
                      "icon-maskable-512.png", "apple-touch-icon.png"}
_AVATAR_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
_AVATAR_RE = re.compile(r"^avatar_[pt]\d+_[0-9a-f]{8}\.(png|jpg|jpeg|webp|gif)$")

# SQL-fragment: weergavenaam = bijnaam als die er is, anders de accountnaam.
WEERGAVE = "COALESCE(NULLIF(p.nickname, ''), p.name)"


# ---------------------------------------------------------------- database --

def get_db():
    if "db" not in g:
        g.db = verbind(DB_PATH)
    return g.db


@app.teardown_appcontext
def sluit_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def controleer_sessie():
    """Een sessie van een speler die niet meer bestaat (bv. na het vernieuwen van
    de spelersnummers) wordt netjes opgeruimd in plaats van fouten te geven."""
    speler_id = session.get("speler_id")
    if speler_id and request.endpoint not in (None, "statisch", "media"):
        bestaat = get_db().execute("SELECT 1 FROM players WHERE id = ?",
                                   (speler_id,)).fetchone()
        if not bestaat:
            session.pop("speler_id", None)


def instelling(db, sleutel, standaard=None):
    rij = db.execute("SELECT value FROM settings WHERE key = ?", (sleutel,)).fetchone()
    return rij["value"] if rij else standaard


def zet_instelling(db, sleutel, waarde):
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (sleutel, str(waarde)))


# ------------------------------------------------------------------- auth --
#
# Er is geen algemeen adminwachtwoord: je bent organisator omdat je eigen account
# die rol heeft. Het eerste account dat wordt aangemaakt is meteen eigenaar.
#
#   speler    — gewone speler, geen organisatierechten
#   admin     — organisator: mag alles behalve andermans organisatorrol afnemen
#   eigenaar  — hoofdorganisator (hoogstens één): mag dat wél, en kan het
#               eigenaarschap doorgeven. Kan door niemand afgezet of gewist worden.

ROLLEN = (ROL_SPELER, ROL_ADMIN, ROL_EIGENAAR)
ORGANISATOREN = (ROL_ADMIN, ROL_EIGENAAR)

ROL_LABEL = {ROL_SPELER: "speler", ROL_ADMIN: "organisator",
             ROL_EIGENAAR: "eigenaar"}


def huidige_speler():
    """De ingelogde speler (of None). Wordt per verzoek één keer opgehaald."""
    if "ik" not in g:
        speler_id = session.get("speler_id")
        g.ik = get_db().execute("SELECT * FROM players WHERE id = ?",
                                (speler_id,)).fetchone() if speler_id else None
    return g.ik


def mijn_rol():
    ik = huidige_speler()
    return ik["role"] if ik else ROL_SPELER


def is_organisator():
    return mijn_rol() in ORGANISATOREN


def is_eigenaar():
    return mijn_rol() == ROL_EIGENAAR


def de_eigenaar(db):
    return db.execute("SELECT * FROM players WHERE role = ?",
                      (ROL_EIGENAAR,)).fetchone()


@app.context_processor
def rollen_in_templates():
    """Zo kunnen de sjablonen `ik`, `is_organisator` en `is_eigenaar` gebruiken."""
    return {"ik": huidige_speler(), "is_organisator": is_organisator(),
            "is_eigenaar": is_eigenaar()}


def login_vereist(f):
    """Organisatorrechten vereist (organisator of eigenaar)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("speler_id"):
            flash("Log eerst in met je eigen account om bij het "
                  "organisatiepaneel te kunnen.", "fout")
            return redirect(url_for("inloggen", volgende=request.path))
        if not is_organisator():
            flash("Je hebt hier geen toegang: enkel organisatoren kunnen bij het "
                  "organisatiepaneel. Vraag een organisator om je die rol te geven.",
                  "fout")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def eigenaar_vereist(f):
    """Enkel de eigenaar (hoofdorganisator)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_eigenaar():
            flash("Enkel de eigenaar kan dat doen.", "fout")
            return redirect(url_for("admin_spelers"))
        return f(*args, **kwargs)
    return wrapper


def speler_vereist(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("speler_id"):
            flash("Log eerst in met je spelersaccount.", "fout")
            return redirect(url_for("inloggen"))
        return f(*args, **kwargs)
    return wrapper


# ------------------------------------------------------------------- hulp --

def weergave(speler):
    """De naam waarmee één speler op de site verschijnt: bijnaam, anders echte naam."""
    return (speler["nickname"] or "").strip() or speler["name"]


def weergavenamen(db):
    """{speler_id: bijnaam of naam} voor alle spelers."""
    return {r["id"]: r["naam"] for r in db.execute(
        f"SELECT p.id, {WEERGAVE} AS naam FROM players p")}


def teamnamen(db):
    return {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM teams")}


def herbereken_alles(db):
    """
    Herbereken ALLE ratings door elke gespeelde wedstrijd chronologisch opnieuw
    af te spelen. Zo blijven de ratings altijd consistent, ook na het
    verwijderen of corrigeren van een resultaat of het wijzigen van K-factoren.

    Er worden twee soorten ratings berekend:

    * de **permanente ELO** (`players.elo` / `teams.elo`): álle wedstrijden
      tellen mee, league én toernooi. Toernooiwedstrijden wegen zwaarder naarmate
      een team verder geraakt (zie `elo.fase_factor`). Shootouts tellen niet mee:
      die beslissen enkel wie doorstoot.
    * de **seizoens-ELO** (`season_ratings`): per seizoen apart, iedereen start
      er opnieuw op 1000, en enkel ligawedstrijden van dat seizoen tellen mee.
    """
    k_s = float(instelling(db, "k_speler", "32"))
    k_t = float(instelling(db, "k_team", "32"))

    team_leden = {r["id"]: (r["player1_id"], r["player2_id"])
                  for r in db.execute("SELECT id, player1_id, player2_id FROM teams")}

    db.execute("DELETE FROM rating_history")
    db.execute("DELETE FROM season_ratings")

    # ---------------------------------------------------------- permanent --
    speler_elos = {r["id"]: START_ELO for r in db.execute("SELECT id FROM players")}
    team_elos = {}

    wedstrijden = db.execute(
        "SELECT * FROM games WHERE status = 'gespeeld' AND team1_id IS NOT NULL "
        "AND team2_id IS NOT NULL ORDER BY played_at, id"
    ).fetchall()

    for w in wedstrijden:
        t1, t2 = w["team1_id"], w["team2_id"]
        if t1 not in team_leden or t2 not in team_leden:
            continue
        if w["fase"] == "shootout":
            continue          # beslissingswedstrijd: raakt de ratings niet aan
        for t in (t1, t2):
            if t not in team_elos:
                a, b = team_leden[t]
                team_elos[t] = (speler_elos[a] + speler_elos[b]) / 2.0

        s1 = {pid: speler_elos[pid] for pid in team_leden[t1]}
        s2 = {pid: speler_elos[pid] for pid in team_leden[t2]}
        winnaar = 1 if w["winner_team_id"] == t1 else 2
        f = fase_factor(w["fase"], w["ronde"])
        # In de knockout betaalt de verliezer maar een deel: zo kost ver
        # geraken nooit ELO. Zie de uitleg bij KO_VERLIES in elo.py.
        f_verlies = fase_factor(w["fase"], w["ronde"], verloren=True)
        deel = (f_verlies / f) if f else 1.0

        nieuwe, nieuw_t1, nieuw_t2 = proces_wedstrijd(s1, s2, team_elos[t1],
                                                      team_elos[t2], winnaar,
                                                      k_s * f, k_t * f, deel)

        for pid, na in nieuwe.items():
            db.execute("INSERT INTO rating_history (game_id, entity_type, entity_id, "
                       "elo_voor, elo_na, scope) VALUES (?, 'speler', ?, ?, ?, 'permanent')",
                       (w["id"], pid, speler_elos[pid], na))
        db.execute("INSERT INTO rating_history (game_id, entity_type, entity_id, "
                   "elo_voor, elo_na, scope) VALUES (?, 'team', ?, ?, ?, 'permanent')",
                   (w["id"], t1, team_elos[t1], nieuw_t1))
        db.execute("INSERT INTO rating_history (game_id, entity_type, entity_id, "
                   "elo_voor, elo_na, scope) VALUES (?, 'team', ?, ?, ?, 'permanent')",
                   (w["id"], t2, team_elos[t2], nieuw_t2))

        speler_elos.update(nieuwe)
        team_elos[t1], team_elos[t2] = nieuw_t1, nieuw_t2

    for pid, e in speler_elos.items():
        db.execute("UPDATE players SET elo = ? WHERE id = ?", (e, pid))
    for tid, (a, b) in team_leden.items():
        e = team_elos.get(tid, (speler_elos[a] + speler_elos[b]) / 2.0)
        db.execute("UPDATE teams SET elo = ? WHERE id = ?", (e, tid))

    # ------------------------------------------------------- per seizoen --
    for s in db.execute("SELECT id FROM seasons").fetchall():
        _herbereken_seizoen(db, s["id"], team_leden, k_s, k_t)

    db.commit()


def _herbereken_seizoen(db, seizoen_id, team_leden, k_s, k_t):
    """Seizoens-ELO: iedereen start op 1000, enkel ligawedstrijden tellen mee."""
    sp, tm = {}, {}
    telling = defaultdict(lambda: {"gespeeld": 0, "winst": 0})

    wedstrijden = db.execute("""
        SELECT g.* FROM games g
        JOIN matchdays md ON md.id = g.matchday_id
        WHERE md.season_id = ? AND g.status = 'gespeeld' AND g.fase = 'liga'
          AND g.team1_id IS NOT NULL AND g.team2_id IS NOT NULL
        ORDER BY g.played_at, g.id
    """, (seizoen_id,)).fetchall()

    for w in wedstrijden:
        t1, t2 = w["team1_id"], w["team2_id"]
        if t1 not in team_leden or t2 not in team_leden:
            continue
        for pid in team_leden[t1] + team_leden[t2]:
            sp.setdefault(pid, START_ELO)
        for t in (t1, t2):
            if t not in tm:
                a, b = team_leden[t]
                tm[t] = (sp[a] + sp[b]) / 2.0

        s1 = {pid: sp[pid] for pid in team_leden[t1]}
        s2 = {pid: sp[pid] for pid in team_leden[t2]}
        winnaar = 1 if w["winner_team_id"] == t1 else 2
        nieuwe, nieuw_t1, nieuw_t2 = proces_wedstrijd(s1, s2, tm[t1], tm[t2],
                                                      winnaar, k_s, k_t)

        for pid, na in nieuwe.items():
            db.execute("INSERT INTO rating_history (game_id, entity_type, entity_id, "
                       "elo_voor, elo_na, scope, season_id) "
                       "VALUES (?, 'speler', ?, ?, ?, 'seizoen', ?)",
                       (w["id"], pid, sp[pid], na, seizoen_id))
        for tid_, voor, na in ((t1, tm[t1], nieuw_t1), (t2, tm[t2], nieuw_t2)):
            db.execute("INSERT INTO rating_history (game_id, entity_type, entity_id, "
                       "elo_voor, elo_na, scope, season_id) "
                       "VALUES (?, 'team', ?, ?, ?, 'seizoen', ?)",
                       (w["id"], tid_, voor, na, seizoen_id))

        for tid_, leden in ((t1, team_leden[t1]), (t2, team_leden[t2])):
            gewonnen = (w["winner_team_id"] == tid_)
            telling[("team", tid_)]["gespeeld"] += 1
            telling[("team", tid_)]["winst"] += gewonnen
            for pid in leden:
                telling[("speler", pid)]["gespeeld"] += 1
                telling[("speler", pid)]["winst"] += gewonnen

        sp.update(nieuwe)
        tm[t1], tm[t2] = nieuw_t1, nieuw_t2

    for pid, e in sp.items():
        d = telling[("speler", pid)]
        db.execute("INSERT INTO season_ratings (season_id, entity_type, entity_id, "
                   "elo, gespeeld, winst) VALUES (?, 'speler', ?, ?, ?, ?)",
                   (seizoen_id, pid, e, d["gespeeld"], d["winst"]))
    for tid_, e in tm.items():
        d = telling[("team", tid_)]
        db.execute("INSERT INTO season_ratings (season_id, entity_type, entity_id, "
                   "elo, gespeeld, winst) VALUES (?, 'team', ?, ?, ?, ?)",
                   (seizoen_id, tid_, e, d["gespeeld"], d["winst"]))


def na_resultaat(db, game_id=None):
    """Alles bijwerken na een nieuw of gewijzigd resultaat."""
    herbereken_alles(db)
    return toernooi_motor.evalueer_alles(db)


def seizoen_klassement(db, seizoen_id):
    """Klassement binnen één seizoen (seizoens-ELO, iedereen startte op 1000)."""
    spelers = db.execute(f"""
        SELECT sr.*, {WEERGAVE} AS naam, p.avatar, p.id AS pid
        FROM season_ratings sr
        JOIN players p ON p.id = sr.entity_id
        WHERE sr.season_id = ? AND sr.entity_type = 'speler'
        ORDER BY sr.elo DESC, naam
    """, (seizoen_id,)).fetchall()
    teams = db.execute("""
        SELECT sr.*, t.name AS naam, t.avatar, t.id AS tid
        FROM season_ratings sr
        JOIN teams t ON t.id = sr.entity_id
        WHERE sr.season_id = ? AND sr.entity_type = 'team'
        ORDER BY sr.elo DESC, naam
    """, (seizoen_id,)).fetchall()
    return spelers, teams


def huidig_seizoen(db):
    vandaag = date.today().isoformat()
    return db.execute("SELECT * FROM seasons WHERE start_date <= ? AND end_date >= ? "
                      "ORDER BY start_date DESC LIMIT 1", (vandaag, vandaag)).fetchone()


def speel_statistieken(db):
    """Gespeeld/gewonnen per speler en per team, berekend uit de wedstrijden.

    Shootouts tellen niet mee: dat zijn beslissingswedstrijden, geen echte
    partijen. Ze blijven wel zichtbaar in de wedstrijdhistoriek.
    """
    rijen = db.execute("""
        SELECT g.winner_team_id, g.team1_id, g.team2_id,
               t1.player1_id AS a1, t1.player2_id AS a2,
               t2.player1_id AS b1, t2.player2_id AS b2
        FROM games g
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        WHERE g.status = 'gespeeld' AND g.fase != 'shootout'
    """).fetchall()
    spelers = defaultdict(lambda: {"gespeeld": 0, "winst": 0})
    teams = defaultdict(lambda: {"gespeeld": 0, "winst": 0})
    for r in rijen:
        for tid, pids in ((r["team1_id"], (r["a1"], r["a2"])),
                          (r["team2_id"], (r["b1"], r["b2"]))):
            gewonnen = (tid == r["winner_team_id"])
            teams[tid]["gespeeld"] += 1
            teams[tid]["winst"] += gewonnen
            for pid in pids:
                spelers[pid]["gespeeld"] += 1
                spelers[pid]["winst"] += gewonnen
    return spelers, teams


def wedstrijden_deze_maand(db):
    """Aantal wedstrijden (gepland + gespeeld) per speler in de huidige maand."""
    prefix = datetime.now().strftime("%Y-%m")
    rijen = db.execute("""
        SELECT COALESCE(g.played_at, g.scheduled_at) AS moment,
               t1.player1_id AS a1, t1.player2_id AS a2,
               t2.player1_id AS b1, t2.player2_id AS b2
        FROM games g
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        WHERE g.fase != 'shootout'
    """).fetchall()
    telling = defaultdict(int)
    for r in rijen:
        if (r["moment"] or "").startswith(prefix):
            for pid in (r["a1"], r["a2"], r["b1"], r["b2"]):
                telling[pid] += 1
    return telling


def alle_rangen(db):
    return db.execute("SELECT * FROM ranks ORDER BY min_elo DESC").fetchall()


def rang_voor(elo, rangen):
    for r in rangen:
        if r["min_elo"] <= elo <= r["max_elo"]:
            return r
    return None


def rangverdeling(db):
    """Hoeveel actieve spelers in elke rang zitten (voor het staafdiagram)."""
    rangen = alle_rangen(db)
    if not rangen:
        return []
    elos = [r["elo"] for r in db.execute("SELECT elo FROM players WHERE active = 1")]
    totaal = len(elos)
    verdeling = []
    zonder = 0
    for e in elos:
        if rang_voor(e, rangen) is None:
            zonder += 1
    for r in sorted(rangen, key=lambda x: -x["min_elo"]):
        aantal = sum(1 for e in elos if rang_voor(e, rangen) is not None
                     and rang_voor(e, rangen)["id"] == r["id"])
        verdeling.append({"naam": r["name"], "kleur": r["color"], "aantal": aantal,
                          "pct": (100.0 * aantal / totaal) if totaal else 0})
    if zonder:
        verdeling.append({"naam": "Zonder rang", "kleur": "#cbd5e1", "aantal": zonder,
                          "pct": (100.0 * zonder / totaal) if totaal else 0})
    return verdeling


def elo_verloop(db, entity_id, entity_type="speler"):
    """Punten voor de ELO-grafiek: na elke wedstrijd de nieuwe ELO + positie.

    Werkt zowel voor een speler als voor een team: de plaats in het klassement
    wordt telkens berekend binnen de eigen soort (spelers onder spelers, teams
    onder teams), net zoals de twee klassementen op de startpagina.

    Klein verschil tussen de twee, dat het teamklassement ook maakt: elke speler
    staat in de ranglijst, maar een team pas zodra het samen gespeeld heeft. Een
    duo dat enkel op papier bestaat, telt dus niet mee voor de plaats.
    """
    is_team = entity_type == "team"
    tabel = "teams" if is_team else "players"
    huidige = {r["id"]: START_ELO for r in db.execute(f"SELECT id FROM {tabel}")}
    gespeeld = set()                       # wie al aan de bak is geweest
    rijen = db.execute("""
        SELECT rh.game_id, rh.entity_id, rh.elo_na, g.played_at
        FROM rating_history rh
        JOIN games g ON g.id = rh.game_id
        WHERE rh.entity_type = ? AND rh.scope = 'permanent'
        ORDER BY g.played_at, g.id
    """, (entity_type,)).fetchall()

    punten = []
    for _game_id, groep in groupby(rijen, key=lambda r: r["game_id"]):
        groep = list(groep)
        deed_mee = False
        moment = None
        for r in groep:
            if r["entity_id"] in huidige:
                huidige[r["entity_id"]] = r["elo_na"]
                gespeeld.add(r["entity_id"])
            if r["entity_id"] == entity_id:
                deed_mee = True
                moment = r["played_at"]
        if deed_mee:
            eigen = huidige[entity_id]
            rang = 1 + sum(1 for wie, v in huidige.items() if v > eigen
                           and (not is_team or wie in gespeeld))
            punten.append({"datum": filter_datum(moment), "elo": round(eigen),
                           "rang": rang})
    return punten


def actieve_wedstrijd(db, speler_id):
    """De eerstvolgende wedstrijd van deze speler, voor de balk onderaan.

    Zo hoeft niemand te zoeken waar hij moet zijn: één tik brengt hem naar het
    formulier om de uitslag te melden.
    """
    g_ = db.execute("""
        SELECT g.id, g.scheduled_at, g.tournament_id, g.team1_id, g.team2_id,
               tn.name AS toernooi, loc.name AS locatie,
               t1.player1_id AS a1, t1.player2_id AS a2,
               t2.player1_id AS b1, t2.player2_id AS b2
        FROM games g
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        LEFT JOIN tournaments tn ON tn.id = g.tournament_id
        LEFT JOIN tournament_locations loc ON loc.id = g.location_id
        WHERE g.status = 'gepland'
          AND ? IN (t1.player1_id, t1.player2_id, t2.player1_id, t2.player2_id)
        ORDER BY g.scheduled_at, g.id LIMIT 1
    """, (speler_id,)).fetchone()
    if not g_:
        return None
    # Een leaguewedstrijd tonen we niet zolang dat gedeelte dicht staat.
    if g_["tournament_id"] is None and not league_toegankelijk():
        return None

    eigen = g_["team1_id"] if speler_id in (g_["a1"], g_["a2"]) else g_["team2_id"]
    tegen = g_["team2_id"] if eigen == g_["team1_id"] else g_["team1_id"]
    # Enkel de naam van dié tegenstander ophalen: deze functie draait bij élke
    # paginaweergave van een ingelogde speler.
    naam_rij = db.execute("SELECT name FROM teams WHERE id = ?", (tegen,)).fetchone()
    gemeld = db.execute("SELECT 1 FROM game_reports WHERE game_id = ? AND team_id = ?",
                        (g_["id"], eigen)).fetchone() is not None
    if g_["tournament_id"]:
        doel = url_for("toernooi_detail", toernooi_id=g_["tournament_id"])
    else:
        doel = url_for("speler_profiel", speler_id=speler_id) + "#melden"
    return {"tegenstander": naam_rij["name"] if naam_rij else "?",
            "locatie": g_["locatie"],
            "wanneer": g_["scheduled_at"], "toernooi": g_["toernooi"],
            "gemeld": gemeld, "url": doel, "game_id": g_["id"]}


def wedstrijd_context(g_):
    """Waar hoort deze wedstrijd bij? Geeft label, soort en link terug."""
    if g_["toernooi"]:
        return {"soort": "toernooi", "naam": g_["toernooi"],
                "detail": fase_omschrijving(g_["fase"], g_["ronde"]),
                "url": url_for("toernooi_detail", toernooi_id=g_["toernooi_id"]),
                "locatie": g_["locatie"]}
    if g_["seizoen"]:
        # Staat de league uit, dan is de seizoenspagina er niet voor spelers:
        # de naam blijft staan, de link valt weg.
        return {"soort": "liga", "naam": g_["seizoen"], "detail": g_["speeldag"],
                "url": (url_for("seizoen_detail", seizoen_id=g_["seizoen_id"])
                        if league_toegankelijk() else None),
                "locatie": None}
    return {"soort": "liga", "naam": "Vriendschappelijk", "detail": None,
            "url": None, "locatie": None}


def speler_wedstrijd_meldingen(db, speler_id, toernooi_id=None, alleen_liga=False,
                               alleen_toernooi=False):
    """Geplande wedstrijden van deze speler, met de meldstatus van beide teams."""
    items = []
    filter_sql = ""
    args = [speler_id]
    if toernooi_id is not None:
        filter_sql = "AND g.tournament_id = ?"
        args.append(toernooi_id)
    elif alleen_liga:
        filter_sql = "AND g.tournament_id IS NULL"
    elif alleen_toernooi:
        filter_sql = "AND g.tournament_id IS NOT NULL"
    for g_ in db.execute(f"""
        SELECT g.*, t1.player1_id AS a1, t1.player2_id AS a2,
               t2.player1_id AS b1, t2.player2_id AS b2,
               md.title AS speeldag, s.name AS seizoen, s.id AS seizoen_id,
               tn.name AS toernooi, tn.id AS toernooi_id, loc.name AS locatie
        FROM games g
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        LEFT JOIN matchdays md ON md.id = g.matchday_id
        LEFT JOIN seasons s ON s.id = md.season_id
        LEFT JOIN tournaments tn ON tn.id = g.tournament_id
        LEFT JOIN tournament_locations loc ON loc.id = g.location_id
        WHERE g.status = 'gepland'
          AND ? IN (t1.player1_id, t1.player2_id, t2.player1_id, t2.player2_id)
          {filter_sql}
        ORDER BY g.scheduled_at
    """, args).fetchall():
        eigen_team = g_["team1_id"] if speler_id in (g_["a1"], g_["a2"]) else g_["team2_id"]
        tegen_team = g_["team2_id"] if eigen_team == g_["team1_id"] else g_["team1_id"]
        meldingen = {r["team_id"]: r for r in db.execute(
            "SELECT * FROM game_reports WHERE game_id = ?", (g_["id"],))}
        eigen = meldingen.get(eigen_team)
        tegen = meldingen.get(tegen_team)
        conflict = bool(eigen and tegen
                        and eigen["winner_team_id"] != tegen["winner_team_id"])
        lid_ids = ((g_["a1"], g_["a2"]) if eigen_team == g_["team1_id"]
                   else (g_["b1"], g_["b2"]))
        leden = db.execute(f"SELECT p.id, {WEERGAVE} AS name FROM players p "
                           "WHERE p.id IN (?, ?)", lid_ids).fetchall()
        waarden = {pid: {} for pid in lid_ids}
        for r in db.execute("SELECT player_id, stat_type_id, value FROM game_stats "
                            "WHERE game_id = ? AND player_id IN (?, ?)",
                            (g_["id"], *lid_ids)):
            waarden[r["player_id"]][r["stat_type_id"]] = r["value"]
        items.append({"game": g_, "eigen_team": eigen_team, "tegen_team": tegen_team,
                      "eigen": eigen, "tegen": tegen, "conflict": conflict,
                      "leden": leden, "waarden": waarden,
                      "context": wedstrijd_context(g_),
                      "heeft_waarden": any(waarden.values())})
    return items


def seizoen_historiek(db, entity_id, entity_type="speler"):
    """Per seizoen: eindplaats in het seizoensklassement, cijfers en ELO.

    Werkt zowel voor een speler als voor een team; de plaats is telkens die
    binnen het klassement van zijn eigen soort.
    """
    uit = []
    for r in db.execute("""
        SELECT s.id, s.name, s.start_date, s.end_date,
               sr.elo, sr.gespeeld, sr.winst,
               (SELECT COUNT(*) FROM season_ratings x
                 WHERE x.season_id = sr.season_id AND x.entity_type = sr.entity_type
                   AND x.elo > sr.elo) + 1 AS plaats,
               (SELECT COUNT(*) FROM season_ratings x
                 WHERE x.season_id = sr.season_id
                   AND x.entity_type = sr.entity_type) AS aantal
        FROM season_ratings sr
        JOIN seasons s ON s.id = sr.season_id
        WHERE sr.entity_type = ? AND sr.entity_id = ?
        ORDER BY s.start_date DESC
    """, (entity_type, entity_id)):
        d = dict(r)
        d["status"] = seizoen_status(r)
        d["verlies"] = r["gespeeld"] - r["winst"]
        uit.append(d)
    return uit


def toernooi_historiek(db, speler_id=None, team_id=None):
    """Per toernooi: met welk team, hoever geraakt en de eindstand.

    Geef ofwel een speler mee (dan tellen alle teams waarin hij speelde),
    ofwel één team.
    """
    from elo import KO_LABEL

    if team_id is not None:
        rijen = db.execute("""
            SELECT tn.id, tn.name, tn.date, tn.status, tn.ko_teams,
                   tt.team_id, t.name AS team_naam
            FROM tournaments tn
            JOIN tournament_teams tt ON tt.tournament_id = tn.id
            JOIN teams t ON t.id = tt.team_id
            WHERE tt.team_id = ?
            ORDER BY tn.date DESC, tn.id DESC
        """, (team_id,))
    else:
        rijen = db.execute("""
            SELECT tn.id, tn.name, tn.date, tn.status, tn.ko_teams,
                   tt.team_id, t.name AS team_naam
            FROM tournaments tn
            JOIN tournament_teams tt ON tt.tournament_id = tn.id
            JOIN teams t ON t.id = tt.team_id
            WHERE t.player1_id = ? OR t.player2_id = ?
            ORDER BY tn.date DESC, tn.id DESC
        """, (speler_id, speler_id))

    uit = []
    for r in rijen:
        tid, team_id = r["id"], r["team_id"]

        cijfers = db.execute("""
            SELECT COUNT(*) AS gespeeld,
                   COALESCE(SUM(winner_team_id = ?), 0) AS winst
            FROM games WHERE tournament_id = ? AND status = 'gespeeld'
              AND fase != 'shootout' AND (team1_id = ? OR team2_id = ?)
        """, (team_id, tid, team_id, team_id)).fetchone()

        positie = aantal = None
        if r["status"] != "opzet":
            geordend, _ = toernooi_motor.stand(db, tid)
            aantal = len(geordend)
            positie = next((x["positie"] for x in geordend
                            if x["team_id"] == team_id), None)

        ko = db.execute("""
            SELECT * FROM games WHERE tournament_id = ? AND fase = 'knockout'
              AND (team1_id = ? OR team2_id = ?) ORDER BY ronde
        """, (tid, team_id, team_id)).fetchall()
        gespeelde_ko = [g for g in ko if g["status"] == "gespeeld"]
        wacht_nog = any(g["status"] == "gepland" for g in ko)

        klasse, label = "", ""
        if r["status"] == "opzet":
            label = "Nog niet geloot"
        elif gespeelde_ko or wacht_nog:
            verst = min(g["ronde"] for g in (gespeelde_ko or ko))
            beslissend = next((g for g in gespeelde_ko if g["ronde"] == verst), None)
            gewonnen = bool(beslissend and beslissend["winner_team_id"] == team_id)
            if wacht_nog:
                volgende = min(g["ronde"] for g in ko if g["status"] == "gepland")
                label = f"Nog in de running · {KO_LABEL.get(volgende, 'knockout').lower()}"
                klasse = "loopt"
            elif verst == 2 and gewonnen:
                label = "🏆 Toernooiwinnaar"
                klasse = "goud"
            elif verst == 2:
                label = "🥈 Finalist"
                klasse = "zilver"
            else:
                label = f"Uitgeschakeld in de {KO_LABEL.get(verst, 'knockout').lower()}"
                klasse = "brons" if verst == 4 else ""
        elif r["status"] == "afgelopen":
            label = ("Net naast de knockout" if positie and positie <= r["ko_teams"] + 2
                     else "Niet naar de knockout")
        else:
            label = "Bracketfase bezig"
            klasse = "loopt"

        uit.append({"id": tid, "naam": r["name"], "datum": r["date"],
                    "status": r["status"], "team_id": team_id,
                    "team_naam": r["team_naam"], "positie": positie,
                    "aantal": aantal, "ko_teams": r["ko_teams"],
                    "gespeeld": cijfers["gespeeld"], "winst": cijfers["winst"],
                    "verlies": cijfers["gespeeld"] - cijfers["winst"],
                    "label": label, "klasse": klasse})
    return uit


def liga_records(db):
    """Vaste "recordboek"-statistieken, berekend uit alle gespeelde wedstrijden."""
    naam_van = {r["id"]: r["naam"] for r in db.execute(
        f"SELECT p.id, {WEERGAVE} AS naam FROM players p WHERE p.active = 1")}
    rijen = db.execute("""
        SELECT g.winner_team_id, g.team1_id, g.team2_id,
               t1.player1_id AS a1, t1.player2_id AS a2,
               t2.player1_id AS b1, t2.player2_id AS b2
        FROM games g
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        WHERE g.status = 'gespeeld' AND g.fase != 'shootout'
        ORDER BY g.played_at, g.id
    """).fetchall()
    if not rijen:
        return []

    sp = defaultdict(lambda: {"g": 0, "w": 0, "reeks": 0, "beste_reeks": 0})
    team_tellen = defaultdict(int)
    for r in rijen:
        team_tellen[r["team1_id"]] += 1
        team_tellen[r["team2_id"]] += 1
        won1 = r["winner_team_id"] == r["team1_id"]
        winnaars = (r["a1"], r["a2"]) if won1 else (r["b1"], r["b2"])
        verliezers = (r["b1"], r["b2"]) if won1 else (r["a1"], r["a2"])
        for pid in winnaars:
            s = sp[pid]
            s["g"] += 1
            s["w"] += 1
            s["reeks"] += 1
            s["beste_reeks"] = max(s["beste_reeks"], s["reeks"])
        for pid in verliezers:
            s = sp[pid]
            s["g"] += 1
            s["reeks"] = 0

    def top(sleutel, filter_fn=None):
        items = [(pid, d) for pid, d in sp.items()
                 if pid in naam_van and (filter_fn is None or filter_fn(d))]
        if not items:
            return None, []
        beste = max(sleutel(d) for _, d in items)
        if beste <= 0:
            return None, []
        return beste, [pid for pid, d in items if sleutel(d) == beste]

    def speler_links(pids):
        return [{"naam": naam_van[p],
                 "url": url_for("speler_profiel", speler_id=p)} for p in pids[:4]]

    records = []

    beste, pids = top(lambda d: d["beste_reeks"])
    if beste:
        records.append({"emoji": "🔥", "titel": "Langste winstreeks",
                        "waarde": f"{beste} op rij", "houders": speler_links(pids)})

    beste, pids = top(lambda d: d["reeks"])
    if beste and beste >= 2:
        records.append({"emoji": "⚡", "titel": "Actuele winstreeks",
                        "waarde": f"{beste} op rij", "houders": speler_links(pids)})

    beste, pids = top(lambda d: d["g"])
    if beste:
        records.append({"emoji": "🎮", "titel": "Meeste wedstrijden",
                        "waarde": f"{beste} gespeeld", "houders": speler_links(pids)})

    beste, pids = top(lambda d: d["w"])
    if beste:
        records.append({"emoji": "🏆", "titel": "Meeste overwinningen",
                        "waarde": f"{beste} gewonnen", "houders": speler_links(pids)})

    beste, pids = top(lambda d: round(100.0 * d["w"] / d["g"], 1),
                      filter_fn=lambda d: d["g"] >= 5)
    if beste:
        records.append({"emoji": "💯", "titel": "Beste winstpercentage",
                        "waarde": f"{beste:.0f}% (min. 5 wedstrijden)",
                        "houders": speler_links(pids)})

    piek = db.execute("""
        SELECT rh.entity_id AS pid, MAX(rh.elo_na) AS v
        FROM rating_history rh
        JOIN players p ON p.id = rh.entity_id AND p.active = 1
        WHERE rh.entity_type = 'speler' AND rh.scope = 'permanent'
    """).fetchone()
    if piek and piek["pid"] is not None:
        records.append({"emoji": "🚀", "titel": "Hoogste ELO ooit",
                        "waarde": f"{piek['v']:.0f}",
                        "houders": speler_links([piek["pid"]])})

    sprong = db.execute("""
        SELECT rh.entity_id AS pid, MAX(rh.elo_na - rh.elo_voor) AS v
        FROM rating_history rh
        JOIN players p ON p.id = rh.entity_id AND p.active = 1
        WHERE rh.entity_type = 'speler' AND rh.scope = 'permanent'
    """).fetchone()
    if sprong and sprong["pid"] is not None and sprong["v"] and sprong["v"] > 0:
        records.append({"emoji": "📈", "titel": "Grootste ELO-sprong",
                        "waarde": f"+{sprong['v']:.0f} in één wedstrijd",
                        "houders": speler_links([sprong["pid"]])})

    if team_tellen:
        namen = teamnamen(db)
        beste = max(team_tellen.values())
        houders = [{"naam": namen[t], "url": url_for("team_profiel", team_id=t)}
                   for t, n in team_tellen.items() if n == beste][:3]
        records.append({"emoji": "👯", "titel": "Onafscheidelijk duo",
                        "waarde": f"{beste} wedstrijden samen", "houders": houders})

    return records


def seizoen_stats(db, seizoen_id):
    """Ereborden van één seizoen: overwinningen en ELO-klim, spelers en teams."""
    rijen = db.execute("""
        SELECT g.id, g.winner_team_id, g.team1_id, g.team2_id,
               t1.player1_id AS a1, t1.player2_id AS a2,
               t2.player1_id AS b1, t2.player2_id AS b2
        FROM games g
        JOIN matchdays md ON md.id = g.matchday_id
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        WHERE md.season_id = ? AND g.status = 'gespeeld'
    """, (seizoen_id,)).fetchall()
    if not rijen:
        return None

    sp = defaultdict(lambda: {"g": 0, "w": 0})
    tm = defaultdict(lambda: {"g": 0, "w": 0})
    for r in rijen:
        for tid, pids in ((r["team1_id"], (r["a1"], r["a2"])),
                          (r["team2_id"], (r["b1"], r["b2"]))):
            gewonnen = (tid == r["winner_team_id"])
            tm[tid]["g"] += 1
            tm[tid]["w"] += gewonnen
            for pid in pids:
                sp[pid]["g"] += 1
                sp[pid]["w"] += gewonnen

    klim = {"speler": defaultdict(float), "team": defaultdict(float)}
    for r in db.execute("""
        SELECT rh.entity_type, rh.entity_id,
               SUM(rh.elo_na - rh.elo_voor) AS d
        FROM rating_history rh
        WHERE rh.scope = 'seizoen' AND rh.season_id = ?
        GROUP BY rh.entity_type, rh.entity_id
    """, (seizoen_id,)):
        klim[r["entity_type"]][r["entity_id"]] = r["d"]

    namen_sp = weergavenamen(db)
    namen_tm = teamnamen(db)

    spelers_winst = [{"naam": namen_sp[pid],
                      "url": url_for("speler_profiel", speler_id=pid),
                      "waarde": f"{d['w']} gewonnen ({d['g']} gespeeld)"}
                     for pid, d in sorted(sp.items(),
                                          key=lambda kv: (-kv[1]["w"], kv[1]["g"]))[:5]
                     if pid in namen_sp]
    spelers_klim = [{"naam": namen_sp[pid],
                     "url": url_for("speler_profiel", speler_id=pid),
                     "waarde": f"{'+' if d >= 0 else ''}{d:.0f} ELO"}
                    for pid, d in sorted(klim["speler"].items(), key=lambda kv: -kv[1])[:5]
                    if pid in namen_sp]
    teams_winst = [{"naam": namen_tm[tid],
                    "url": url_for("team_profiel", team_id=tid),
                    "waarde": f"{d['w']} gewonnen ({d['g']} gespeeld)"}
                   for tid, d in sorted(tm.items(), key=lambda kv: -kv[1]["w"])[:3]
                   if tid in namen_tm]
    teams_klim = [{"naam": namen_tm[tid],
                   "url": url_for("team_profiel", team_id=tid),
                   "waarde": f"{'+' if d >= 0 else ''}{d:.0f} ELO"}
                  for tid, d in sorted(klim["team"].items(), key=lambda kv: -kv[1])[:3]
                  if tid in namen_tm]

    return {"aantal": len(rijen), "spelers_winst": spelers_winst,
            "spelers_klim": spelers_klim, "teams_winst": teams_winst,
            "teams_klim": teams_klim}


def wis_avatar(bestandsnaam):
    """Verwijder een profielfoto van de schijf (als het er echt eentje is)."""
    if bestandsnaam and _AVATAR_RE.match(bestandsnaam):
        try:
            os.remove(os.path.join(UPLOAD_MAP, bestandsnaam))
        except OSError:
            pass


def bewaar_avatar(bestand, prefix, oud=None):
    """Sla een geüploade profielfoto op; geeft de bestandsnaam terug (of None)."""
    if not bestand or not bestand.filename or "." not in bestand.filename:
        return None
    ext = bestand.filename.rsplit(".", 1)[-1].lower()
    if ext not in _AVATAR_EXT:
        return None
    # Grootte zelf nakijken: de algemene uploadlimiet staat hoog voor back-ups.
    bestand.stream.seek(0, os.SEEK_END)
    if bestand.stream.tell() > MAX_AVATAR:
        bestand.stream.seek(0)
        return None
    bestand.stream.seek(0)
    os.makedirs(UPLOAD_MAP, exist_ok=True)
    naam = f"avatar_{prefix}_{secrets.token_hex(4)}.{ext}"
    bestand.save(os.path.join(UPLOAD_MAP, naam))
    if oud and _AVATAR_RE.match(oud):
        try:
            os.remove(os.path.join(UPLOAD_MAP, oud))
        except OSError:
            pass
    return naam


# ---------------------------------------------------------------- filters --

@app.template_filter("elo")
def filter_elo(waarde):
    return f"{waarde:.0f}"


@app.template_filter("kracht")
def filter_kracht(waarde):
    """Toernooikracht: de ELO die je vanavond won of verloor, mét teken.

    Twee cijfers na de komma: dit staat er enkel als het cijfer de volgorde
    bepaalt, en dan mogen twee ploegen die net niet gelijk staan er niet gelijk
    uitzien. De `or 0.0` houdt een lelijke "-0,00" weg.
    """
    afgerond = round(waarde or 0.0, 2) or 0.0
    return f"{afgerond:+.2f}".replace(".", ",")


@app.template_filter("datum")
def filter_datum(iso):
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    return f"{d.day} {MAANDEN[d.month - 1]} {d.year}, {d:%H:%M}"


@app.template_filter("uur")
def filter_uur(iso):
    """Enkel het uur — genoeg voor de balk met je eerstvolgende wedstrijd."""
    if not iso:
        return "—"
    try:
        return f"{datetime.fromisoformat(iso):%H:%M}"
    except (ValueError, TypeError):
        return iso


@app.template_filter("dag")
def filter_dag(iso):
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    return f"{d.day} {MAANDEN[d.month - 1]} {d.year}"


@app.template_filter("maandlabel")
def filter_maandlabel(iso):
    try:
        d = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return ""
    return f"{MAANDEN[d.month - 1].capitalize()} {d.year}"


@app.template_filter("fase")
def filter_fase(game):
    """Leesbaar label van de fase van een wedstrijd."""
    try:
        return fase_omschrijving(game["fase"], game["ronde"])
    except (TypeError, KeyError, IndexError):
        return "Wedstrijd"


# ------------------------------------------------------- league aan of uit --
#
# De site kan draaien met enkel het toernooigedeelte. Eén vinkje bij
# Organisatie → Instellingen zet het leaguegedeelte (klassement, wedstrijden,
# statistieken, seizoenen) aan of uit voor de spelers. Organisatoren houden
# altijd toegang, zodat je alles rustig kan klaarzetten voor je opengaat.

def league_zichtbaar():
    """Mag de league getoond worden aan gewone bezoekers en spelers?"""
    return instelling(get_db(), "league_actief", "0") == "1"


def league_toegankelijk():
    """Mag ík de leaguepagina's zien? Organisatoren ook als de league uit staat."""
    return league_zichtbaar() or is_organisator()


def league_vereist(f):
    """Leaguepagina: onzichtbaar voor spelers zolang het gedeelte uit staat."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not league_toegankelijk():
            return redirect(url_for("toernooien"))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def extra_context():
    naam = "logo.png" if os.path.exists(os.path.join(STATIC_MAP, "logo.png")) else "logo.svg"
    pad = request.path if request else "/"
    league_uit = not league_zichtbaar()
    # Zonder league is er maar één deel; dan is alles wat geen toernooi is
    # (profielen, inloggen …) toch onder "toernooi" te vinden.
    sectie = "toernooi" if (pad.startswith("/toernooi") or league_uit) else "liga"
    # De balk met je eerstvolgende wedstrijd: enkel voor wie ingelogd is, en niet
    # bij statische bestanden (die hoeven geen databasevraag).
    nu_spelen = None
    if session.get("speler_id") and request.endpoint not in (
            None, "statisch", "media", "service_worker", "manifest"):
        try:
            nu_spelen = actieve_wedstrijd(get_db(), session["speler_id"])
        except Exception:
            nu_spelen = None            # nooit een pagina laten crashen hierdoor

    return {"logo_bestand": naam, "sectie": sectie,
            "league_zichtbaar": league_zichtbaar(),
            "league_toegankelijk": league_toegankelijk(),
            "claim_venster_open": claim_open(),
            "nu_spelen": nu_spelen}


def seizoen_status(s):
    vandaag = date.today().isoformat()
    if s["end_date"] < vandaag:
        return "afgelopen"
    if s["start_date"] > vandaag:
        return "gepland"
    return "bezig"


# ----------------------------------------------------------------- static --

@app.route("/static/<path:bestand>")
def statisch(bestand):
    if os.path.basename(bestand) not in _TOEGELATEN_STATIC:
        abort(404)
    return send_from_directory(STATIC_MAP, os.path.basename(bestand))


def _static_versie():
    """Een korte code die verandert zodra style.css of app.js wijzigt.

    De service worker bewaart die vaste bestanden en haalt ze daarna uit zijn
    eigen cache. Zonder deze code zou een bezoeker na een update nog altijd de
    óude stijl zien: de cache wordt namelijk pas opgeruimd als de naam ervan
    verandert. Door de inhoud van de bestanden te hashen gebeurt dat vanzelf,
    en hoeven we bij elke aanpassing niets handmatig op te hogen.
    """
    stempel = hashlib.sha1()
    for naam in sorted(_TOEGELATEN_STATIC):
        pad = os.path.join(STATIC_MAP, naam)
        try:
            with open(pad, "rb") as f:
                stempel.update(f.read())
        except OSError:
            continue
    return stempel.hexdigest()[:12]


@app.route("/sw.js")
def service_worker():
    """De service worker moet vanaf de hoofdmap geserveerd worden, anders mag hij
    enkel /static/ beheren en werkt de app niet offline."""
    with open(os.path.join(STATIC_MAP, "sw.js"), encoding="utf-8") as f:
        inhoud = f.read()
    # De cachenaam meegeven, zodat een update de oude bestanden weggooit.
    inhoud = inhoud.replace("__VERSIE__", _static_versie())
    antwoord = app.response_class(inhoud, mimetype="application/javascript")
    antwoord.headers["Service-Worker-Allowed"] = "/"
    antwoord.headers["Cache-Control"] = "no-cache"
    return antwoord


@app.route("/manifest.webmanifest")
def manifest():
    """Het manifest maakt de site installeerbaar als app op de gsm.

    We serveren het via een route (en niet als vast bestand), omdat de
    startpagina afhangt van de instellingen: staat de league uit, dan is dat de
    toernooipagina. Een start_url die doorverwijst, keurt Chrome soms af — dan
    verschijnt de installatieknop niet.
    """
    with open(os.path.join(STATIC_MAP, "manifest.webmanifest"), encoding="utf-8") as f:
        gegevens = json.load(f)
    start = "/" if league_zichtbaar() else url_for("toernooien")
    gegevens["start_url"] = start
    gegevens["shortcuts"] = [s for s in gegevens.get("shortcuts", [])
                             if league_zichtbaar() or s["url"] != "/"]
    antwoord = app.response_class(json.dumps(gegevens, ensure_ascii=False, indent=2),
                                  mimetype="application/manifest+json")
    antwoord.headers["Cache-Control"] = "no-cache"
    return antwoord


@app.route("/media/<bestand>")
def media(bestand):
    if not _AVATAR_RE.match(bestand):
        abort(404)
    return send_from_directory(UPLOAD_MAP, bestand)


# --------------------------------------------------------- publieke pagina's

@app.route("/")
def index():
    if not league_toegankelijk():
        # Zonder league is de toernooipagina de thuispagina.
        return redirect(url_for("toernooien"))
    db = get_db()
    sp_stats, tm_stats = speel_statistieken(db)
    rangen = alle_rangen(db)

    spelers = []
    for p in db.execute(f"SELECT p.*, {WEERGAVE} AS naam FROM players p "
                        "WHERE p.active = 1 ORDER BY p.elo DESC, naam"):
        s = sp_stats.get(p["id"], {"gespeeld": 0, "winst": 0})
        spelers.append({
            "id": p["id"], "naam": p["naam"], "elo": p["elo"], "avatar": p["avatar"],
            "gespeeld": s["gespeeld"], "winst": s["winst"],
            "verlies": s["gespeeld"] - s["winst"],
            "pct": (100.0 * s["winst"] / s["gespeeld"]) if s["gespeeld"] else None,
            "rang": rang_voor(p["elo"], rangen),
        })

    teams = []
    for t in db.execute("SELECT * FROM teams WHERE status = 'actief' ORDER BY elo DESC"):
        s = tm_stats.get(t["id"])
        if not s:
            continue  # enkel teams die al effectief samen gespeeld hebben
        teams.append({
            "id": t["id"], "naam": t["name"], "elo": t["elo"], "avatar": t["avatar"],
            "gespeeld": s["gespeeld"], "winst": s["winst"],
            "verlies": s["gespeeld"] - s["winst"],
            "pct": 100.0 * s["winst"] / s["gespeeld"],
        })
    teams.sort(key=lambda x: -x["elo"])

    aantal_gespeeld = db.execute(
        "SELECT COUNT(*) AS n FROM games WHERE status = 'gespeeld' "
        "AND fase != 'shootout'").fetchone()["n"]

    return render_template("index.html", spelers=spelers, teams=teams,
                           aantal_spelers=len(spelers), aantal_teams=len(teams),
                           aantal_gespeeld=aantal_gespeeld)


@app.route("/wedstrijden")
@league_vereist
def wedstrijden():
    db = get_db()
    namen = teamnamen(db)

    gepland = db.execute(
        "SELECT * FROM games WHERE status = 'gepland' AND tournament_id IS NULL "
        "ORDER BY scheduled_at").fetchall()
    gespeeld = db.execute(
        "SELECT * FROM games WHERE status = 'gespeeld' AND tournament_id IS NULL "
        "ORDER BY played_at DESC, id DESC").fetchall()

    deltas = {}
    for r in db.execute("SELECT game_id, entity_id, elo_voor, elo_na FROM rating_history "
                        "WHERE entity_type = 'team' AND scope = 'permanent'"):
        deltas[(r["game_id"], r["entity_id"])] = r["elo_na"] - r["elo_voor"]

    speeldag_labels = {}
    for r in db.execute("""
        SELECT g.id, md.title, s.name AS seizoen
        FROM games g
        JOIN matchdays md ON md.id = g.matchday_id
        JOIN seasons s ON s.id = md.season_id
    """):
        speeldag_labels[r["id"]] = f'{r["title"]} · {r["seizoen"]}'

    stats_per_game = defaultdict(list)
    for r in db.execute(f"""
        SELECT gs.game_id, {WEERGAVE} AS speler, st.name AS stat, st.unit, gs.value
        FROM game_stats gs
        JOIN players p ON p.id = gs.player_id
        JOIN stat_types st ON st.id = gs.stat_type_id
        ORDER BY st.name, speler
    """):
        stats_per_game[r["game_id"]].append(r)

    return render_template("wedstrijden.html", gepland=gepland, gespeeld=gespeeld,
                           namen=namen, deltas=deltas, stats_per_game=stats_per_game,
                           speeldag_labels=speeldag_labels)


@app.route("/statistieken")
@league_vereist
def statistieken():
    db = get_db()
    types = db.execute("SELECT * FROM stat_types WHERE active = 1 ORDER BY name").fetchall()

    borden = []
    for st in types:
        rijen = db.execute(f"""
            SELECT p.id AS speler_id, {WEERGAVE} AS naam, SUM(gs.value) AS totaal,
                   COUNT(DISTINCT gs.game_id) AS n
            FROM game_stats gs
            JOIN players p ON p.id = gs.player_id
            WHERE gs.stat_type_id = ?
            GROUP BY gs.player_id
        """, (st["id"],)).fetchall()
        totaal = sorted(rijen, key=lambda r: -r["totaal"])[:10]
        gemiddeld = sorted(rijen, key=lambda r: -(r["totaal"] / r["n"]))[:10]
        borden.append({"type": st, "totaal": totaal, "gemiddeld": gemiddeld})

    return render_template("statistieken.html", borden=borden,
                           records=liga_records(db), verdeling=rangverdeling(db))


@app.route("/seizoenen")
@league_vereist
def seizoenen():
    db = get_db()
    lijst = []
    for s in db.execute("SELECT * FROM seasons ORDER BY start_date DESC"):
        speeldagen = db.execute("SELECT COUNT(*) AS n FROM matchdays "
                                "WHERE season_id = ?", (s["id"],)).fetchone()["n"]
        gespeeld = db.execute("""
            SELECT COUNT(*) AS n FROM games g
            JOIN matchdays md ON md.id = g.matchday_id
            WHERE md.season_id = ? AND g.status = 'gespeeld'
        """, (s["id"],)).fetchone()["n"]
        stats = seizoen_stats(db, s["id"]) if gespeeld else None
        lijst.append({"seizoen": s, "status": seizoen_status(s),
                      "speeldagen": speeldagen, "gespeeld": gespeeld,
                      "topklimmer": (stats["spelers_klim"][0] if stats and
                                     stats["spelers_klim"] else None)})
    return render_template("seizoenen.html", lijst=lijst)


@app.route("/seizoen/<int:seizoen_id>")
@league_vereist
def seizoen_detail(seizoen_id):
    db = get_db()
    seizoen = db.execute("SELECT * FROM seasons WHERE id = ?", (seizoen_id,)).fetchone()
    if not seizoen:
        abort(404)
    namen = teamnamen(db)

    speeldagen = []
    for md in db.execute("SELECT * FROM matchdays WHERE season_id = ? ORDER BY date",
                         (seizoen_id,)):
        games = db.execute("SELECT * FROM games WHERE matchday_id = ? "
                           "ORDER BY scheduled_at", (md["id"],)).fetchall()
        speeldagen.append({"speeldag": md, "games": games})

    sp_klassement, tm_klassement = seizoen_klassement(db, seizoen_id)

    return render_template("seizoen.html", seizoen=seizoen,
                           status=seizoen_status(seizoen), speeldagen=speeldagen,
                           namen=namen, stats=seizoen_stats(db, seizoen_id),
                           sp_klassement=sp_klassement, tm_klassement=tm_klassement)


# --------------------------------------------------------------- profielen --

@app.route("/speler/<int:speler_id>")
def speler_profiel(speler_id):
    db = get_db()
    speler = db.execute(f"SELECT p.*, {WEERGAVE} AS naam FROM players p "
                        "WHERE p.id = ?", (speler_id,)).fetchone()
    if not speler:
        abort(404)

    sp_stats, tm_stats = speel_statistieken(db)
    s = sp_stats.get(speler_id, {"gespeeld": 0, "winst": 0})
    rangen = alle_rangen(db)
    namen = teamnamen(db)
    namen_sp = weergavenamen(db)

    is_eigen = (session.get("speler_id") == speler_id)

    # Teams van deze speler (actief); voor de eigenaar ook de uitnodigingen.
    teams = []
    uitnodigingen = []   # ontvangen (te beantwoorden)
    verstuurd = []       # zelf verstuurd, wacht op antwoord
    for t in db.execute("SELECT * FROM teams WHERE player1_id = ? OR player2_id = ? "
                        "ORDER BY elo DESC", (speler_id, speler_id)):
        partner_id = t["player2_id"] if t["player1_id"] == speler_id else t["player1_id"]
        info = {"id": t["id"], "naam": t["name"], "elo": t["elo"],
                "avatar": t["avatar"], "beschrijving": t["description"],
                "partner": namen_sp.get(partner_id, "?"), "partner_id": partner_id}
        if t["status"] == "actief":
            ts = tm_stats.get(t["id"], {"gespeeld": 0, "winst": 0})
            info["gespeeld"] = ts["gespeeld"]
            info["winst"] = ts["winst"]
            teams.append(info)
        elif is_eigen and t["player2_id"] == speler_id:
            uitnodigingen.append(info)
        elif is_eigen:
            verstuurd.append(info)

    eigen_stats = db.execute("""
        SELECT st.name, st.unit, SUM(gs.value) AS totaal,
               COUNT(DISTINCT gs.game_id) AS n
        FROM game_stats gs
        JOIN stat_types st ON st.id = gs.stat_type_id
        WHERE gs.player_id = ? AND st.active = 1
        GROUP BY st.id ORDER BY st.name
    """, (speler_id,)).fetchall()

    historiek = []
    for r in db.execute("""
        SELECT g.*, rh.elo_voor, rh.elo_na
        FROM games g
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        LEFT JOIN rating_history rh ON rh.game_id = g.id
             AND rh.entity_type = 'speler' AND rh.entity_id = ? AND rh.scope = 'permanent'
        WHERE g.status = 'gespeeld'
          AND ? IN (t1.player1_id, t1.player2_id, t2.player1_id, t2.player2_id)
        ORDER BY g.played_at DESC, g.id DESC
    """, (speler_id, speler_id)):
        leden = db.execute("SELECT player1_id, player2_id FROM teams WHERE id = ?",
                           (r["team1_id"],)).fetchone()
        eigen_team = (r["team1_id"] if speler_id in (leden["player1_id"],
                                                     leden["player2_id"])
                      else r["team2_id"])
        tegen_team = r["team2_id"] if eigen_team == r["team1_id"] else r["team1_id"]
        historiek.append({
            "game": r, "eigen_team": eigen_team, "tegen_team": tegen_team,
            "gewonnen": r["winner_team_id"] == eigen_team,
            "delta": (r["elo_na"] - r["elo_voor"]) if r["elo_na"] is not None else None,
        })

    punten = elo_verloop(db, speler_id)
    # Staat de league uit, dan mag een speler ook geen leaguewedstrijden melden.
    meldingen = (speler_wedstrijd_meldingen(db, speler_id,
                                            alleen_toernooi=not league_toegankelijk())
                 if is_eigen else [])
    actieve_types = (db.execute("SELECT * FROM stat_types WHERE active = 1 "
                                "ORDER BY name").fetchall() if is_eigen else [])
    # Mogelijke partners voor een nieuw team.
    partners = (db.execute(f"""
        SELECT p.id, {WEERGAVE} AS naam FROM players p
        WHERE p.active = 1 AND p.id != ? ORDER BY naam
    """, (speler_id,)).fetchall() if is_eigen else [])

    # Palmares: per seizoen de eindplaats, per toernooi hoever hij geraakte.
    seizoen_ratings = seizoen_historiek(db, speler_id)
    toernooien_gespeeld = toernooi_historiek(db, speler_id)

    # Toernooicijfers (tellen wel mee voor de permanente ELO).
    tn = db.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN g.winner_team_id =
                        (CASE WHEN ? IN (t1.player1_id, t1.player2_id)
                              THEN g.team1_id ELSE g.team2_id END)
                   THEN 1 ELSE 0 END) AS w
        FROM games g
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        WHERE g.status = 'gespeeld' AND g.tournament_id IS NOT NULL
          AND g.fase != 'shootout'
          AND ? IN (t1.player1_id, t1.player2_id, t2.player1_id, t2.player2_id)
    """, (speler_id, speler_id)).fetchone()
    toernooi_stats = {"gespeeld": tn["n"] or 0, "winst": tn["w"] or 0,
                      "toernooien": toernooien_gespeeld,
                      "titels": sum(1 for x in toernooien_gespeeld
                                    if x["klasse"] == "goud")}

    return render_template("speler.html", speler=speler, stats=s,
                           seizoen_ratings=seizoen_ratings,
                           toernooien=toernooien_gespeeld,
                           titels=toernooi_stats["titels"],
                           toernooi_stats=toernooi_stats,
                           verlies=s["gespeeld"] - s["winst"],
                           pct=(100.0 * s["winst"] / s["gespeeld"]) if s["gespeeld"] else None,
                           rang=rang_voor(speler["elo"], rangen), teams=teams,
                           uitnodigingen=uitnodigingen, verstuurd=verstuurd,
                           eigen_stats=eigen_stats, historiek=historiek,
                           namen=namen, punten=punten, is_eigen=is_eigen,
                           meldingen=meldingen, actieve_types=actieve_types,
                           partners=partners)


@app.route("/team/<int:team_id>")
def team_profiel(team_id):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        abort(404)
    is_lid = session.get("speler_id") in (team["player1_id"], team["player2_id"])
    if team["status"] != "actief" and not (is_lid or is_organisator()):
        abort(404)

    _, tm_stats = speel_statistieken(db)
    s = tm_stats.get(team_id, {"gespeeld": 0, "winst": 0})
    namen = teamnamen(db)
    leden = db.execute(f"SELECT p.id, {WEERGAVE} AS name, p.elo, p.avatar "
                       "FROM players p WHERE p.id IN (?, ?)",
                       (team["player1_id"], team["player2_id"])).fetchall()

    historiek = []
    for r in db.execute("""
        SELECT g.*, rh.elo_voor, rh.elo_na
        FROM games g
        LEFT JOIN rating_history rh ON rh.game_id = g.id
             AND rh.entity_type = 'team' AND rh.entity_id = ? AND rh.scope = 'permanent'
        WHERE g.status = 'gespeeld' AND (g.team1_id = ? OR g.team2_id = ?)
        ORDER BY g.played_at DESC, g.id DESC
    """, (team_id, team_id, team_id)):
        tegen = r["team2_id"] if r["team1_id"] == team_id else r["team1_id"]
        historiek.append({
            "game": r, "tegen_team": tegen,
            "gewonnen": r["winner_team_id"] == team_id,
            "delta": (r["elo_na"] - r["elo_voor"]) if r["elo_na"] is not None else None,
        })

    heeft_games = db.execute("SELECT 1 FROM games WHERE team1_id = ? OR team2_id = ? "
                             "LIMIT 1", (team_id, team_id)).fetchone() is not None

    # Palmares van het team: eindplaats per seizoen en resultaat per toernooi.
    seizoen_ratings = seizoen_historiek(db, team_id, "team")
    toernooien_team = toernooi_historiek(db, team_id=team_id)
    titels = sum(1 for x in toernooien_team if x["klasse"] == "goud")

    return render_template("team.html", team=team, naam=team["name"], leden=leden,
                           stats=s, verlies=s["gespeeld"] - s["winst"],
                           pct=(100.0 * s["winst"] / s["gespeeld"]) if s["gespeeld"] else None,
                           historiek=historiek, namen=namen, is_lid=is_lid,
                           heeft_games=heeft_games,
                           punten=elo_verloop(db, team_id, "team"),
                           seizoen_ratings=seizoen_ratings,
                           toernooien=toernooien_team, titels=titels)


# ------------------------------------------------------------ spelersaccounts

@app.route("/registreren", methods=["GET", "POST"])
def registreren():
    db = get_db()
    # Het állereerste account wordt eigenaar: iemand moet de organisatie kunnen doen.
    eerste = db.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"] == 0
    if request.method == "POST":
        naam = " ".join((request.form.get("naam") or "").split())
        bijnaam = (request.form.get("bijnaam") or "").strip()
        ww = request.form.get("wachtwoord") or ""
        if len(naam) < 3 or len(naam) > 40:
            flash("Vul je echte voor- en achternaam in (3 tot 40 tekens).", "fout")
        elif len(bijnaam) > 30:
            flash("Hou je bijnaam korter dan 30 tekens.", "fout")
        elif len(ww) < 6:
            flash("Kies een wachtwoord van minstens 6 tekens.", "fout")
        else:
            try:
                nummer = vrij_spelernummer(db)
                db.execute("INSERT INTO players (id, name, nickname, password_hash, "
                           "role, elo) VALUES (?, ?, ?, ?, ?, ?)",
                           (nummer, naam, bijnaam, generate_password_hash(ww),
                            ROL_EIGENAAR if eerste else ROL_SPELER, START_ELO))
                db.commit()
                session["speler_id"] = nummer
                flash(f"Welkom bij Leberschuss Tonzent, {bijnaam or naam}! Je "
                      f"spelersnummer is #{nummer} en je start op {START_ELO:.0f} ELO. "
                      "Maak of aanvaard een team om mee te spelen.", "ok")
                if eerste:
                    flash("Jij bent het eerste account en dus meteen de eigenaar van "
                          "deze site: jij kan anderen organisator maken via het "
                          "organisatiepaneel.", "ok")
                return redirect(url_for("speler_profiel", speler_id=nummer))
            except Exception:
                db.rollback()
                flash("Er bestaat al een account met die naam. Ben jij dat? Log dan "
                      "in. Anders: zet er iets bij, bv. je tweede voornaam.", "fout")
    return render_template("registreren.html", eerste=eerste)


@app.route("/inloggen", methods=["GET", "POST"])
def inloggen():
    if request.method == "POST":
        ingave = " ".join((request.form.get("naam") or "").split())
        ww = request.form.get("wachtwoord") or ""
        db = get_db()
        # Inloggen kan met je echte naam of met je spelersnummer (bv. 4821).
        sleutel = ingave.lstrip("#")
        speler = db.execute(
            "SELECT * FROM players WHERE name = ? COLLATE NOCASE OR id = ?",
            (ingave, int(sleutel) if sleutel.isdigit() else -1)).fetchone()
        if speler and speler["password_hash"] and check_password_hash(speler["password_hash"], ww):
            session["speler_id"] = speler["id"]
            g.pop("ik", None)
            flash(f"Welkom terug, {weergave(speler)}!", "ok")
            volgende = request.args.get("volgende") or ""
            if volgende.startswith("/") and not volgende.startswith("//"):
                return redirect(volgende)
            return redirect(url_for("speler_profiel", speler_id=speler["id"]))
        flash("Naam of wachtwoord klopt niet.", "fout")
    return render_template("inloggen.html")


# ------------------------------------------------------ account opeisen --
#
# De organisator maakt de spelers vooraf aan (uit de inschrijvingen), zonder
# wachtwoord. De speler zelf kiest er later één: hij zoekt zijn naam op /claimen
# en stelt een wachtwoord in. Dat venster staat enkel open wanneer de organisatie
# het openzet — in de praktijk terwijl ze erbij staan. Elke claim wordt gelogd,
# zodat een vergissing zichtbaar is en teruggedraaid kan worden.

def claim_open():
    return instelling(get_db(), "claim_open", "0") == "1"


def externe_url(endpoint, **kwargs):
    """Het volledige adres van een pagina, zoals een bezoeker het moet intypen.

    Flask leidt dat af uit het adres waarmee de pagina opgevraagd werd — dus uit
    het domein waarmee jij het organisatiepaneel opent. Draai je achter nginx,
    dan zorgt ACHTER_PROXY=1 ervoor dat https ook klopt.
    """
    return url_for(endpoint, _external=True, **kwargs)


def qr_svg(tekst, rand=3):
    """Een QR-code als inline SVG, schaalbaar via CSS (None als segno ontbreekt)."""
    try:
        import segno
    except ImportError:
        return None
    code = segno.make(tekst, error="m")
    breedte, hoogte = code.symbol_size(scale=1, border=rand)
    svg = code.svg_inline(scale=1, border=rand, dark="#0f172a", light=None)
    # De vaste afmetingen weg, een viewBox erin: zo vult de code zijn kader.
    svg = re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)
    return Markup(svg.replace(
        "<svg ", f'<svg viewBox="0 0 {breedte} {hoogte}" '
                 'preserveAspectRatio="xMidYMid meet" ', 1))


def vrije_accounts(db):
    """Accounts zonder wachtwoord: die wachten nog op hun eigenaar."""
    return db.execute(
        f"SELECT p.id, p.name, p.nickname, {WEERGAVE} AS naam FROM players p "
        "WHERE p.password_hash IS NULL ORDER BY p.name COLLATE NOCASE").fetchall()


def _log_claim(db, speler, soort):
    db.execute("INSERT INTO claim_log (player_id, naam, soort, ip) VALUES (?, ?, ?, ?)",
               (speler["id"], speler["name"], soort,
                (request.headers.get("X-Forwarded-For") or request.remote_addr or "")
                .split(",")[0].strip()))


def _claim_venster_dicht():
    """Antwoord voor wie langskomt terwijl het opeisen niet openstaat."""
    flash("Accounts opeisen staat momenteel niet open. Vraag het even aan de "
          "organisatie.", "fout")
    return redirect(url_for("inloggen"))


@app.route("/claimen")
def claimen():
    """Stap 1: kies je naam uit de lijst."""
    if not claim_open():
        return _claim_venster_dicht()
    return render_template("claimen.html", accounts=vrije_accounts(get_db()))


@app.route("/claimen/<int:speler_id>", methods=["GET", "POST"])
def claim_account(speler_id):
    """Stap 2: bijnaam en wachtwoord kiezen voor de naam die je aanklikte."""
    db = get_db()
    if not claim_open():
        return _claim_venster_dicht()
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    if not speler:
        flash("Die speler bestaat niet (meer). Kies je naam uit de lijst.", "fout")
        return redirect(url_for("claimen"))
    if speler["password_hash"]:
        # Iemand anders was sneller, of dit account was al van iemand.
        flash(f"“{weergave(speler)}” heeft ondertussen al een wachtwoord. Ben jij dat "
              "en lukt inloggen niet? Vraag de organisatie om je account vrij te "
              "geven.", "fout")
        return redirect(url_for("claimen"))

    if request.method == "POST":
        ww = request.form.get("wachtwoord") or ""
        herhaal = request.form.get("herhaal") or ""
        bijnaam = (request.form.get("bijnaam") or "").strip()
        if len(ww) < 6:
            flash("Kies een wachtwoord van minstens 6 tekens.", "fout")
        elif ww != herhaal:
            flash("De twee wachtwoorden zijn niet hetzelfde.", "fout")
        elif len(bijnaam) > 30:
            flash("Hou je bijnaam korter dan 30 tekens.", "fout")
        else:
            # Laatste controle vlak vóór het opslaan: enkel wie nog geen wachtwoord
            # heeft, krijgt er hier één. Zo wint bij twee gelijktijdige pogingen
            # altijd de eerste, en overschrijft de tweede niets.
            klaar = db.execute("UPDATE players SET password_hash = ?, nickname = ? "
                               "WHERE id = ? AND password_hash IS NULL",
                               (generate_password_hash(ww),
                                bijnaam or speler["nickname"], speler_id)).rowcount
            if not klaar:
                db.rollback()
                flash(f"Net te laat: iemand koos zonet al een wachtwoord voor "
                      f"“{weergave(speler)}”. Vraag het even aan de organisatie.", "fout")
                return redirect(url_for("claimen"))
            _log_claim(db, speler, "claim")
            db.commit()
            session["speler_id"] = speler_id
            g.pop("ik", None)
            flash(f"Welkom, {bijnaam or weergave(speler)}! Dit account is nu van jou. "
                  "Onthoud je wachtwoord — je logt voortaan in met "
                  f"“{speler['name']}” of met #{speler_id}.", "ok")
            return redirect(url_for("speler_profiel", speler_id=speler_id))

    return render_template("claim_account.html", speler=speler)


@app.route("/uitloggen")
def uitloggen():
    session.pop("speler_id", None)
    flash("Je bent uitgelogd.", "ok")
    return redirect(url_for("index"))


@app.route("/profiel/instellingen", methods=["POST"])
@speler_vereist
def profiel_instellingen():
    db = get_db()
    speler_id = session["speler_id"]
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()

    bijnaam = (request.form.get("bijnaam") or "").strip()
    if len(bijnaam) > 30:
        flash("Hou je bijnaam korter dan 30 tekens.", "fout")
        return redirect(url_for("speler_profiel", speler_id=speler_id))
    db.execute("UPDATE players SET nickname = ? WHERE id = ?", (bijnaam, speler_id))

    bestand = request.files.get("avatar")
    if bestand and bestand.filename:
        naam = bewaar_avatar(bestand, f"p{speler_id}", oud=speler["avatar"])
        if naam:
            db.execute("UPDATE players SET avatar = ? WHERE id = ?", (naam, speler_id))
        else:
            flash("Die profielfoto kon niet opgeslagen worden (toegelaten: "
                  "png, jpg, webp of gif, max. 3 MB).", "fout")
    db.commit()
    flash("Je profiel is bijgewerkt.", "ok")
    return redirect(url_for("speler_profiel", speler_id=speler_id))


@app.route("/team/nieuw", methods=["POST"])
@speler_vereist
def team_nieuw():
    db = get_db()
    speler_id = session["speler_id"]
    naam = (request.form.get("naam") or "").strip()
    beschrijving = (request.form.get("beschrijving") or "").strip()[:200]
    try:
        partner_id = int(request.form.get("partner", ""))
    except ValueError:
        flash("Kies een teamgenoot.", "fout")
        return redirect(url_for("speler_profiel", speler_id=speler_id))

    partner = db.execute("SELECT * FROM players WHERE id = ? AND active = 1",
                         (partner_id,)).fetchone()
    if not partner or partner_id == speler_id:
        flash("Kies een geldige teamgenoot.", "fout")
    elif not naam or len(naam) > 40:
        flash("Geef je team een naam van maximaal 40 tekens.", "fout")
    elif db.execute("SELECT 1 FROM teams WHERE (player1_id = ? AND player2_id = ?) "
                    "OR (player1_id = ? AND player2_id = ?)",
                    (speler_id, partner_id, partner_id, speler_id)).fetchone():
        flash("Jullie twee hebben al een team (of een openstaande uitnodiging).", "fout")
    elif db.execute("SELECT 1 FROM teams WHERE name = ?", (naam,)).fetchone():
        flash("Er bestaat al een team met die naam.", "fout")
    else:
        eigen_elo = db.execute("SELECT elo FROM players WHERE id = ?",
                               (speler_id,)).fetchone()["elo"]
        db.execute("INSERT INTO teams (name, description, player1_id, player2_id, "
                   "status, elo) VALUES (?, ?, ?, ?, 'in_afwachting', ?)",
                   (naam, beschrijving, speler_id, partner_id,
                    (eigen_elo + partner["elo"]) / 2.0))
        db.commit()
        flash(f"Team “{naam}” aangemaakt — zodra "
              f"{partner['nickname'] or partner['name']} de uitnodiging aanvaardt, "
              "kunnen jullie wedstrijden spelen.", "ok")
    return redirect(url_for("speler_profiel", speler_id=speler_id))


@app.route("/team/<int:team_id>/antwoord", methods=["POST"])
@speler_vereist
def team_antwoord(team_id):
    db = get_db()
    speler_id = session["speler_id"]
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team or team["status"] != "in_afwachting" or team["player2_id"] != speler_id:
        flash("Deze uitnodiging bestaat niet (meer).", "fout")
        return redirect(url_for("speler_profiel", speler_id=speler_id))

    if request.form.get("actie") == "accepteren":
        db.execute("UPDATE teams SET status = 'actief' WHERE id = ?", (team_id,))
        db.commit()
        flash(f"Je maakt nu deel uit van team “{team['name']}”. Veel succes!", "ok")
    else:
        db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        db.commit()
        flash("Uitnodiging geweigerd.", "ok")
    return redirect(url_for("speler_profiel", speler_id=speler_id))


@app.route("/team/<int:team_id>/bewerken", methods=["POST"])
@speler_vereist
def team_bewerken(team_id):
    db = get_db()
    speler_id = session["speler_id"]
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    # De twee spelers beheren hun eigen team; een organisator mag bijspringen om
    # een schrijffout of een ongepaste naam recht te zetten.
    if not team or not (speler_id in (team["player1_id"], team["player2_id"])
                        or is_organisator()):
        abort(403)
    # Vanuit het organisatiepaneel komt enkel een naam mee: dan laten we de
    # beschrijving met rust in plaats van ze leeg te maken.
    terug = request.form.get("terug") or ""
    # Enkel een pad op deze site: anders kan iemand je via een formulier naar
    # een vreemde website sturen.
    if not terug.startswith("/") or terug.startswith("//"):
        terug = url_for("team_profiel", team_id=team_id)

    naam = (request.form.get("naam") or "").strip()
    if not naam or len(naam) > 40:
        flash("Geef het team een naam van maximaal 40 tekens.", "fout")
        return redirect(terug)
    bestaat = db.execute("SELECT 1 FROM teams WHERE name = ? AND id != ?",
                         (naam, team_id)).fetchone()
    if bestaat:
        flash("Er bestaat al een team met die naam.", "fout")
        return redirect(terug)

    db.execute("UPDATE teams SET name = ? WHERE id = ?", (naam, team_id))
    if "beschrijving" in request.form:
        db.execute("UPDATE teams SET description = ? WHERE id = ?",
                   ((request.form.get("beschrijving") or "").strip()[:200], team_id))

    bestand = request.files.get("avatar")
    if bestand and bestand.filename:
        nieuw = bewaar_avatar(bestand, f"t{team_id}", oud=team["avatar"])
        if nieuw:
            db.execute("UPDATE teams SET avatar = ? WHERE id = ?", (nieuw, team_id))
        else:
            flash("Die teamfoto kon niet opgeslagen worden (toegelaten: "
                  "png, jpg, webp of gif, max. 3 MB).", "fout")
    db.commit()
    flash(f"Team heet nu “{naam}”." if naam != team["name"] else "Team bijgewerkt.", "ok")
    return redirect(terug)


@app.route("/team/<int:team_id>/opheffen", methods=["POST"])
@speler_vereist
def team_opheffen(team_id):
    db = get_db()
    speler_id = session["speler_id"]
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team or speler_id not in (team["player1_id"], team["player2_id"]):
        abort(403)
    heeft_games = db.execute("SELECT 1 FROM games WHERE team1_id = ? OR team2_id = ? "
                             "LIMIT 1", (team_id, team_id)).fetchone()
    if heeft_games:
        flash("Dit team heeft al wedstrijden en kan niet opgeheven worden.", "fout")
        return redirect(url_for("team_profiel", team_id=team_id))
    db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    db.commit()
    flash(f"Team “{team['name']}” is opgeheven.", "ok")
    return redirect(url_for("speler_profiel", speler_id=speler_id))


@app.route("/wedstrijd/<int:game_id>/melden", methods=["POST"])
def wedstrijd_melden(game_id):
    speler_id = session.get("speler_id")
    if not speler_id:
        flash("Log eerst in om een uitslag te melden.", "fout")
        return redirect(url_for("inloggen"))
    db = get_db()

    # Terugkeren naar de pagina waar het formulier stond (profiel of toernooi).
    volgende = request.form.get("volgende") or ""
    terug = (volgende if volgende.startswith("/") and not volgende.startswith("//")
             else url_for("speler_profiel", speler_id=speler_id))
    game = db.execute("""
        SELECT g.*, t1.player1_id AS a1, t1.player2_id AS a2,
               t2.player1_id AS b1, t2.player2_id AS b2
        FROM games g
        JOIN teams t1 ON t1.id = g.team1_id
        JOIN teams t2 ON t2.id = g.team2_id
        WHERE g.id = ?
    """, (game_id,)).fetchone()
    if not game or game["status"] != "gepland":
        flash("Deze wedstrijd bestaat niet of is al afgerond.", "fout")
        return redirect(terug)
    if speler_id not in (game["a1"], game["a2"], game["b1"], game["b2"]):
        flash("Je speelt zelf niet mee in deze wedstrijd.", "fout")
        return redirect(terug)
    if game["tournament_id"] is None and not league_toegankelijk():
        flash("Het leaguegedeelte staat momenteel niet open; deze wedstrijd kan je "
              "nog niet melden.", "fout")
        return redirect(terug)

    eigen_team = game["team1_id"] if speler_id in (game["a1"], game["a2"]) else game["team2_id"]
    tegen_team = game["team2_id"] if eigen_team == game["team1_id"] else game["team1_id"]

    keuze = request.form.get("winnaar")
    if keuze not in ("wij", "zij"):
        flash("Duid aan wie gewonnen heeft.", "fout")
        return redirect(terug)
    winnaar_team = eigen_team if keuze == "wij" else tegen_team

    db.execute("""
        INSERT INTO game_reports (game_id, team_id, player_id, winner_team_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(game_id, team_id) DO UPDATE SET
            player_id = excluded.player_id,
            winner_team_id = excluded.winner_team_id,
            created_at = datetime('now', 'localtime')
    """, (game_id, eigen_team, speler_id, winnaar_team))

    # Statistieken van het eigen team (beide spelers, optioneel, geen invloed op ELO).
    lid_ids = ({game["a1"], game["a2"]} if eigen_team == game["team1_id"]
               else {game["b1"], game["b2"]})
    for veld, waarde in request.form.items():
        if not veld.startswith("stat_"):
            continue
        delen = veld.split("_")
        if len(delen) != 3:
            continue
        try:
            stat_id, lid_id = int(delen[1]), int(delen[2])
        except ValueError:
            continue
        if lid_id not in lid_ids:
            continue  # enkel statistieken van je eigen teamleden
        if waarde.strip():
            try:
                db.execute("INSERT OR REPLACE INTO game_stats (game_id, player_id, "
                           "stat_type_id, value) VALUES (?, ?, ?, ?)",
                           (game_id, lid_id, stat_id,
                            float(waarde.replace(",", "."))))
            except ValueError:
                continue
        else:
            db.execute("DELETE FROM game_stats WHERE game_id = ? AND player_id = ? "
                       "AND stat_type_id = ?", (game_id, lid_id, stat_id))
    db.commit()

    tegen = db.execute("SELECT * FROM game_reports WHERE game_id = ? AND team_id = ?",
                       (game_id, tegen_team)).fetchone()
    if tegen is None:
        flash("Je melding is geregistreerd. Zodra iemand van het andere team dezelfde "
              "uitslag meldt, wordt het resultaat officieel.", "ok")
    elif tegen["winner_team_id"] == winnaar_team:
        db.execute("UPDATE games SET status = 'gespeeld', winner_team_id = ?, "
                   "played_at = ? WHERE id = ?",
                   (winnaar_team, datetime.now().strftime("%Y-%m-%dT%H:%M"), game_id))
        db.commit()
        for melding in na_resultaat(db, game_id):
            flash(melding, "ok")
        flash("Beide teams meldden dezelfde uitslag — het resultaat is officieel en "
              "de ELO-ratings zijn bijgewerkt!", "ok")
    else:
        flash("Oei: het andere team meldde een andere winnaar. Het resultaat blijft "
              "open tot de meldingen overeenkomen of een organisator beslist.", "fout")
    return redirect(terug)


# ------------------------------------------------------------------ admin --

# ------------------------------------------------------------------ rollen --
#
# Wie mag wat? Een organisator mag nieuwe organisatoren aanduiden en mag zijn
# eigen rol teruggeven, maar kan een collega er niet uitzetten. Enkel de
# eigenaar kan dat — en die kan het eigenaarschap doorgeven wanneer hij stopt.

OVERDRACHTSWOORD = "overdragen"


@app.route("/admin/spelers/<int:speler_id>/organisator", methods=["POST"])
@login_vereist
def rol_toekennen(speler_id):
    """Een gewone speler organisator maken. Elke organisator mag dit."""
    db = get_db()
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    if not speler:
        flash("Speler niet gevonden.", "fout")
    elif speler["role"] in ORGANISATOREN:
        flash(f"“{weergave(speler)}” is al {ROL_LABEL[speler['role']]}.", "fout")
    elif not speler["password_hash"]:
        flash(f"“{weergave(speler)}” heeft nog geen wachtwoord en kan dus niet "
              "inloggen als organisator.", "fout")
    else:
        db.execute("UPDATE players SET role = ? WHERE id = ?", (ROL_ADMIN, speler_id))
        db.commit()
        flash(f"“{weergave(speler)}” is nu organisator en kan bij het "
              "organisatiepaneel.", "ok")
    return redirect(url_for("admin_spelers") + "#organisatoren")


@app.route("/admin/spelers/<int:speler_id>/rol-afnemen", methods=["POST"])
@login_vereist
def rol_afnemen(speler_id):
    """Organisatorrol afnemen: van jezelf mag altijd, van een ander enkel als eigenaar."""
    db = get_db()
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    zelf = speler_id == session.get("speler_id")
    if not speler:
        flash("Speler niet gevonden.", "fout")
    elif speler["role"] == ROL_EIGENAAR:
        flash("De eigenaar kan zijn rol niet afgeven. Draag eerst het "
              "eigenaarschap over aan een andere organisator.", "fout")
    elif speler["role"] != ROL_ADMIN:
        flash(f"“{weergave(speler)}” is geen organisator.", "fout")
    elif not zelf and not is_eigenaar():
        flash("Enkel de eigenaar kan de rol van een andere organisator afnemen. "
              "Je kan wel je eigen rol teruggeven.", "fout")
    else:
        db.execute("UPDATE players SET role = ? WHERE id = ?", (ROL_SPELER, speler_id))
        db.commit()
        if zelf:
            flash("Je hebt je organisatorrol teruggegeven. Je bent nu een gewone "
                  "speler.", "ok")
            return redirect(url_for("index"))
        flash(f"“{weergave(speler)}” is geen organisator meer.", "ok")
    return redirect(url_for("admin_spelers") + "#organisatoren")


@app.route("/admin/spelers/<int:speler_id>/eigenaarschap", methods=["POST"])
@login_vereist
@eigenaar_vereist
def eigenaarschap_overdragen(speler_id):
    """Het eigenaarschap doorgeven — bv. als je stopt bij de organisatie.

    De vorige eigenaar wordt gewoon organisator, zodat er nooit twee eigenaars
    zijn en er ook nooit géén eigenaar is.
    """
    db = get_db()
    nieuw = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    woord = (request.form.get("bevestiging") or "").strip().lower()
    if not nieuw:
        flash("Speler niet gevonden.", "fout")
    elif woord != OVERDRACHTSWOORD:
        flash(f"Typ “{OVERDRACHTSWOORD}” in het vakje om de overdracht te "
              "bevestigen. Er is niets gewijzigd.", "fout")
    elif nieuw["role"] != ROL_ADMIN:
        flash("Je kan het eigenaarschap enkel doorgeven aan iemand die al "
              "organisator is. Maak die persoon eerst organisator.", "fout")
    else:
        db.execute("UPDATE players SET role = ? WHERE role = ?",
                   (ROL_ADMIN, ROL_EIGENAAR))
        db.execute("UPDATE players SET role = ? WHERE id = ?",
                   (ROL_EIGENAAR, speler_id))
        db.commit()
        flash(f"“{weergave(nieuw)}” is nu de eigenaar. Jij blijft organisator, "
              "maar kan de rol van anderen niet meer afnemen.", "ok")
    return redirect(url_for("admin_spelers") + "#organisatoren")


# Het organisatiepaneel is opgesplitst in aparte pagina's; deze helpers halen
# telkens enkel op wat die ene pagina nodig heeft.

def _meldingen_per_game(db, alleen_liga=True):
    uit = defaultdict(list)
    filter_sql = "WHERE g.tournament_id IS NULL" if alleen_liga else ""
    for r in db.execute(f"""
        SELECT gr.*, {WEERGAVE} AS speler_naam
        FROM game_reports gr
        JOIN players p ON p.id = gr.player_id
        JOIN games g ON g.id = gr.game_id
        {filter_sql}
        ORDER BY gr.created_at
    """):
        uit[r["game_id"]].append(r)
    return uit


def _team_spelers(db):
    uit = {}
    for t in db.execute("SELECT * FROM teams"):
        rijen = db.execute(f"SELECT p.id, {WEERGAVE} AS name FROM players p "
                           "WHERE p.id IN (?, ?)",
                           (t["player1_id"], t["player2_id"])).fetchall()
        uit[t["id"]] = [dict(x) for x in rijen]
    return uit


def _conflicten(db):
    """Wedstrijden waar de twee teams een andere winnaar meldden."""
    return db.execute("""
        SELECT COUNT(*) AS n FROM (
            SELECT gr.game_id FROM game_reports gr
            JOIN games g ON g.id = gr.game_id AND g.status = 'gepland'
            GROUP BY gr.game_id HAVING COUNT(DISTINCT gr.winner_team_id) > 1
        )
    """).fetchone()["n"]


@app.route("/admin")
@login_vereist
def admin():
    """Startpagina van het organisatiepaneel: overzicht en wegwijzers."""
    db = get_db()

    def tel(sql, *args):
        return db.execute(sql, args).fetchone()["n"]

    cijfers = {
        "spelers": tel("SELECT COUNT(*) AS n FROM players WHERE active = 1"),
        "teams": tel("SELECT COUNT(*) AS n FROM teams WHERE status = 'actief'"),
        "seizoenen": tel("SELECT COUNT(*) AS n FROM seasons"),
        "toernooien": tel("SELECT COUNT(*) AS n FROM tournaments"),
        "open_liga": tel("SELECT COUNT(*) AS n FROM games WHERE status = 'gepland' "
                         "AND tournament_id IS NULL"),
        "open_toernooi": tel("SELECT COUNT(*) AS n FROM games WHERE status = 'gepland' "
                             "AND tournament_id IS NOT NULL AND team1_id IS NOT NULL"),
        "gespeeld": tel("SELECT COUNT(*) AS n FROM games WHERE status = 'gespeeld' "
                        "AND fase != 'shootout'"),
        "conflicten": _conflicten(db),
        "uitnodigingen": tel("SELECT COUNT(*) AS n FROM teams "
                             "WHERE status = 'in_afwachting'"),
    }
    lopend = db.execute("""
        SELECT * FROM tournaments WHERE status IN ('bracket', 'knockout')
        ORDER BY date DESC
    """).fetchall()
    huidig = huidig_seizoen(db)
    return render_template("admin.html", cijfers=cijfers, lopend=lopend,
                           huidig_seizoen=huidig)


@app.route("/admin/seizoenen")
@login_vereist
def admin_seizoenen():
    db = get_db()
    seizoenen_lijst = []
    for s in db.execute("SELECT * FROM seasons ORDER BY start_date DESC"):
        speeldagen = db.execute("""
            SELECT md.*, (SELECT COUNT(*) FROM games g WHERE g.matchday_id = md.id)
                   AS aantal_games
            FROM matchdays md WHERE md.season_id = ? ORDER BY md.date
        """, (s["id"],)).fetchall()
        seizoenen_lijst.append({"seizoen": s, "status": seizoen_status(s),
                                "speeldagen": speeldagen})
    return render_template("admin_seizoenen.html", seizoenen_lijst=seizoenen_lijst)


@app.route("/admin/wedstrijden")
@login_vereist
def admin_wedstrijden():
    db = get_db()
    namen_sp = weergavenamen(db)
    teams = [{"id": t["id"], "naam": t["name"],
              "leden": f'{namen_sp.get(t["player1_id"], "?")} & '
                       f'{namen_sp.get(t["player2_id"], "?")}'}
             for t in db.execute("SELECT * FROM teams WHERE status = 'actief' "
                                 "ORDER BY name")]
    gepland = db.execute(
        "SELECT * FROM games WHERE status = 'gepland' AND tournament_id IS NULL "
        "ORDER BY scheduled_at").fetchall()
    recent = db.execute(
        "SELECT * FROM games WHERE status = 'gespeeld' AND tournament_id IS NULL "
        "ORDER BY played_at DESC, id DESC LIMIT 20").fetchall()
    speeldag_opties = db.execute("""
        SELECT md.id, md.title, md.date, s.name AS seizoen
        FROM matchdays md JOIN seasons s ON s.id = md.season_id
        ORDER BY md.date DESC
    """).fetchall()
    actieve_types = db.execute("SELECT * FROM stat_types WHERE active = 1 "
                               "ORDER BY name").fetchall()
    return render_template("admin_wedstrijden.html", teams=teams, gepland=gepland,
                           recent=recent, speeldag_opties=speeldag_opties,
                           actieve_types=actieve_types, namen=teamnamen(db),
                           team_spelers=_team_spelers(db),
                           meldingen_per_game=_meldingen_per_game(db),
                           nu=datetime.now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/admin/toernooien")
@login_vereist
def admin_toernooien():
    db = get_db()
    toernooien_lijst = db.execute("""
        SELECT tn.*,
               (SELECT COUNT(*) FROM tournament_teams tt
                WHERE tt.tournament_id = tn.id) AS aantal_teams,
               (SELECT COUNT(*) FROM games g
                WHERE g.tournament_id = tn.id AND g.status = 'gepland'
                  AND g.team1_id IS NOT NULL) AS open_games
        FROM tournaments tn ORDER BY tn.date DESC, tn.id DESC
    """).fetchall()
    return render_template("admin_toernooien.html",
                           toernooien_lijst=toernooien_lijst,
                           vandaag=date.today().isoformat())


@app.route("/admin/spelers")
@login_vereist
def admin_spelers():
    db = get_db()
    maand = wedstrijden_deze_maand(db)
    ik_id = session.get("speler_id")
    spelers = []
    for r in db.execute(f"SELECT p.*, {WEERGAVE} AS naam FROM players p ORDER BY naam"):
        d = dict(r)
        d["deze_maand"] = maand.get(r["id"], 0)
        eigen_teams = teams_van_speler(db, r["id"])
        d["teams"] = [t["name"] for t in eigen_teams]
        d["ben_ik"] = r["id"] == ik_id
        d["rol_label"] = ROL_LABEL.get(r["role"], r["role"])
        d["organisator"] = r["role"] in ORGANISATOREN
        # Wie de rol van deze speler mag wijzigen (zie de uitleg bij de auth-helpers).
        d["mag_promoveren"] = (r["role"] == ROL_SPELER and bool(r["password_hash"]))
        d["mag_degraderen"] = (r["role"] == ROL_ADMIN
                               and (is_eigenaar() or d["ben_ik"]))
        d["mag_eigenaar_maken"] = is_eigenaar() and r["role"] == ROL_ADMIN
        # De eigenaar blijft onaantastbaar; enkel de eigenaar raakt aan een organisator.
        d["mag_beheren"] = (r["role"] != ROL_EIGENAAR
                            and (is_eigenaar() or r["role"] == ROL_SPELER))
        d["wis_blokkade"] = None
        if d["ben_ik"]:
            d["wis_blokkade"] = "Je kan je eigen account niet verwijderen."
        elif r["role"] == ROL_EIGENAAR:
            d["wis_blokkade"] = ("De eigenaar kan niet verwijderd worden. Laat het "
                                 "eigenaarschap eerst overdragen.")
        elif not d["mag_beheren"]:
            d["wis_blokkade"] = ("Deze speler is organisator. Enkel de eigenaar kan "
                                 "een organisator verwijderen.")
        elif eigen_teams:
            d["wis_blokkade"] = ("Zit nog in " + ", ".join(d["teams"])
                                 + ". Verwijder die teams eerst.")
        d["verwijderbaar"] = d["wis_blokkade"] is None
        spelers.append(d)

    namen_sp = weergavenamen(db)
    teams = []
    for t in db.execute("SELECT * FROM teams ORDER BY status, name"):
        bezig = team_blokkerende_toernooien(db, t["id"])
        aantal = db.execute("SELECT COUNT(*) AS n FROM games WHERE team1_id = ? "
                            "OR team2_id = ?", (t["id"], t["id"])).fetchone()["n"]
        teams.append({"id": t["id"], "naam": t["name"], "status": t["status"],
                      "elo": t["elo"], "avatar": t["avatar"], "wedstrijden": aantal,
                      "blokkerend": [x["name"] for x in bezig],
                      "leden": f'{namen_sp.get(t["player1_id"], "?")} & '
                               f'{namen_sp.get(t["player2_id"], "?")}'})
    log = db.execute("SELECT * FROM claim_log ORDER BY moment DESC, id DESC "
                     "LIMIT 40").fetchall()
    return render_template("admin_spelers.html", spelers=spelers, teams=teams,
                           claim_open=claim_open(), claim_log=log,
                           claim_adres=externe_url("claimen"),
                           vrij=[p for p in spelers if not p["password_hash"]],
                           maand_label=f"{MAANDEN[datetime.now().month - 1]} "
                                       f"{datetime.now().year}")


@app.route("/admin/klassement")
@login_vereist
def admin_klassement():
    db = get_db()
    return render_template(
        "admin_klassement.html", rangen=alle_rangen(db),
        types=db.execute("SELECT * FROM stat_types ORDER BY active DESC, name").fetchall())


@app.route("/admin/instellingen", methods=["GET"])
@login_vereist
@eigenaar_vereist
def admin_instellingen():
    db = get_db()
    return render_template("admin_instellingen.html",
                           k_speler=instelling(db, "k_speler", "32"),
                           k_team=instelling(db, "k_team", "32"),
                           start_elo=f"{START_ELO:.0f}",
                           backups=backup_lijst()[:6],
                           backup_map=BACKUP_MAP)


@app.route("/admin/seizoenen/nieuw", methods=["POST"])
@login_vereist
def seizoen_nieuw():
    naam = (request.form.get("naam") or "").strip()
    start = request.form.get("start") or ""
    einde = request.form.get("einde") or ""
    if not naam or not start or not einde or start >= einde:
        flash("Geef een naam op en zorg dat de startdatum vóór de einddatum valt.", "fout")
        return redirect(url_for("admin_seizoenen"))
    db = get_db()
    try:
        db.execute("INSERT INTO seasons (name, start_date, end_date) VALUES (?, ?, ?)",
                   (naam, start, einde))
        db.commit()
        flash(f"Seizoen “{naam}” aangemaakt. Voeg nu speeldagen toe.", "ok")
    except Exception:
        flash(f"Er bestaat al een seizoen met de naam “{naam}”.", "fout")
    return redirect(url_for("admin_seizoenen"))


@app.route("/admin/seizoenen/<int:seizoen_id>/verwijderen", methods=["POST"])
@login_vereist
def seizoen_verwijderen(seizoen_id):
    db = get_db()
    db.execute("DELETE FROM seasons WHERE id = ?", (seizoen_id,))
    db.commit()
    flash("Seizoen verwijderd. Gespeelde wedstrijden blijven bestaan, maar horen "
          "niet langer bij een seizoen.", "ok")
    return redirect(url_for("admin_seizoenen"))


@app.route("/admin/speeldagen/nieuw", methods=["POST"])
@login_vereist
def speeldag_nieuw():
    try:
        seizoen_id = int(request.form.get("seizoen", ""))
    except ValueError:
        flash("Kies een seizoen.", "fout")
        return redirect(url_for("admin_seizoenen"))
    datum = request.form.get("datum") or ""
    db = get_db()
    seizoen = db.execute("SELECT * FROM seasons WHERE id = ?", (seizoen_id,)).fetchone()
    if not seizoen or not datum:
        flash("Kies een geldig seizoen en een datum.", "fout")
        return redirect(url_for("admin_seizoenen"))
    titel = (request.form.get("titel") or "").strip()
    if not titel:
        n = db.execute("SELECT COUNT(*) AS n FROM matchdays WHERE season_id = ?",
                       (seizoen_id,)).fetchone()["n"]
        titel = f"Speeldag {n + 1}"
    db.execute("INSERT INTO matchdays (season_id, title, date) VALUES (?, ?, ?)",
               (seizoen_id, titel, datum))
    db.commit()
    flash(f"“{titel}” toegevoegd aan {seizoen['name']}.", "ok")
    return redirect(url_for("admin_seizoenen"))


@app.route("/admin/speeldagen/<int:speeldag_id>/verwijderen", methods=["POST"])
@login_vereist
def speeldag_verwijderen(speeldag_id):
    db = get_db()
    heeft_games = db.execute("SELECT 1 FROM games WHERE matchday_id = ? LIMIT 1",
                             (speeldag_id,)).fetchone()
    if heeft_games:
        flash("Deze speeldag heeft al wedstrijden en kan niet verwijderd worden.", "fout")
    else:
        db.execute("DELETE FROM matchdays WHERE id = ?", (speeldag_id,))
        db.commit()
        flash("Speeldag verwijderd.", "ok")
    return redirect(url_for("admin_seizoenen"))


@app.route("/admin/spelers/nieuw", methods=["POST"])
@login_vereist
def speler_toevoegen():
    """Een speler aanmaken zonder wachtwoord (bv. uit de inschrijvingen).

    De speler kan zo nog niet inloggen; hij kiest zelf een wachtwoord door zijn
    account op te eisen via /claimen. Ondertussen kan je hem al in een team en
    in een toernooi zetten.
    """
    db = get_db()
    naam = " ".join((request.form.get("naam") or "").split())
    bijnaam = (request.form.get("bijnaam") or "").strip()
    if len(naam) < 3 or len(naam) > 40:
        flash("Vul de echte naam in (3 tot 40 tekens).", "fout")
    elif len(bijnaam) > 30:
        flash("Hou de bijnaam korter dan 30 tekens.", "fout")
    else:
        try:
            nummer = vrij_spelernummer(db)
            db.execute("INSERT INTO players (id, name, nickname, elo) VALUES (?, ?, ?, ?)",
                       (nummer, naam, bijnaam, START_ELO))
            db.commit()
            flash(f"“{bijnaam or naam}” toegevoegd met nummer #{nummer}. Er is nog "
                  "geen wachtwoord: de speler kiest er zelf één zodra je het "
                  "opeisen openzet.", "ok")
        except Exception:
            db.rollback()
            flash(f"Er bestaat al een speler met de naam “{naam}”.", "fout")
    return redirect(url_for("admin_spelers") + "#spelers")


@app.route("/admin/teams/nieuw", methods=["POST"])
@login_vereist
def team_toevoegen():
    """Meteen een actief team van twee spelers vormen, zonder uitnodiging."""
    db = get_db()
    naam = (request.form.get("naam") or "").strip()
    try:
        p1 = int(request.form.get("speler1", ""))
        p2 = int(request.form.get("speler2", ""))
    except ValueError:
        flash("Kies twee spelers.", "fout")
        return redirect(url_for("admin_spelers") + "#teams")

    spelers = {r["id"]: r for r in db.execute(
        "SELECT * FROM players WHERE id IN (?, ?)", (p1, p2))}
    if p1 == p2 or len(spelers) != 2:
        flash("Kies twee verschillende, bestaande spelers.", "fout")
    elif not naam or len(naam) > 40:
        flash("Geef het team een naam van maximaal 40 tekens.", "fout")
    elif db.execute("SELECT 1 FROM teams WHERE (player1_id = ? AND player2_id = ?) "
                    "OR (player1_id = ? AND player2_id = ?)", (p1, p2, p2, p1)).fetchone():
        flash("Die twee spelers vormen al een team.", "fout")
    elif db.execute("SELECT 1 FROM teams WHERE name = ?", (naam,)).fetchone():
        flash(f"Er bestaat al een team met de naam “{naam}”.", "fout")
    else:
        db.execute("INSERT INTO teams (name, player1_id, player2_id, status, elo) "
                   "VALUES (?, ?, ?, 'actief', ?)",
                   (naam, p1, p2, (spelers[p1]["elo"] + spelers[p2]["elo"]) / 2.0))
        db.commit()
        flash(f"Team “{naam}” aangemaakt met {weergave(spelers[p1])} en "
              f"{weergave(spelers[p2])}. Het is meteen actief en kan meespelen.", "ok")
    return redirect(url_for("admin_spelers") + "#teams")


@app.route("/admin/spelers/<int:speler_id>/verwijderen", methods=["POST"])
@login_vereist
def speler_verwijderen(speler_id):
    """Zet een speler op inactief of terug op actief (historiek blijft bewaard)."""
    db = get_db()
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    if not speler:
        flash("Speler niet gevonden.", "fout")
        return redirect(url_for("admin_spelers"))
    bezwaar = rol_blokkade(speler)
    if bezwaar:
        flash(bezwaar, "fout")
        return redirect(url_for("admin_spelers"))

    db.execute("UPDATE players SET active = 1 - active WHERE id = ?", (speler_id,))
    db.commit()
    nu_actief = db.execute("SELECT active FROM players WHERE id = ?",
                           (speler_id,)).fetchone()["active"]
    if nu_actief:
        flash(f"“{speler['name']}” is opnieuw geactiveerd.", "ok")
    else:
        flash(f"“{speler['name']}” is gedeactiveerd en verdwijnt uit het klassement; "
              "zijn wedstrijden en historiek blijven bewaard.", "ok")
    return redirect(url_for("admin_spelers"))


BEVESTIGINGSWOORD = "verwijder"


def _bevestigd():
    """Heeft de organisator het woord 'verwijder' echt ingetypt?"""
    return (request.form.get("bevestiging") or "").strip().lower() == BEVESTIGINGSWOORD


def teams_van_speler(db, speler_id):
    """Alle teams (actief of niet) waar deze speler in zit."""
    return db.execute("SELECT * FROM teams WHERE player1_id = ? OR player2_id = ? "
                      "ORDER BY name", (speler_id, speler_id)).fetchall()


def rol_blokkade(speler):
    """Waarom mag ik niet aan dit account komen? (None = geen bezwaar.)

    De eigenaar is voor iedereen onaantastbaar, een organisator enkel voor de
    eigenaar. Anders zou een organisator een collega kunnen buitenzetten door
    diens wachtwoord te resetten of zijn account te wissen.
    """
    if speler["id"] == session.get("speler_id"):
        return None
    if speler["role"] == ROL_EIGENAAR:
        return ("Dit is de eigenaar van de site: zijn account kan niet door een "
                "organisator gewijzigd worden.")
    if speler["role"] == ROL_ADMIN and not is_eigenaar():
        return (f"“{weergave(speler)}” is organisator. Enkel de eigenaar kan aan "
                "het account van een organisator.")
    return None


def team_blokkerende_toernooien(db, team_id):
    """Lopende toernooien waarin dit team meespeelt: die mag je niet stukmaken."""
    return db.execute("""
        SELECT tn.name FROM tournaments tn
        JOIN tournament_teams tt ON tt.tournament_id = tn.id
        WHERE tt.team_id = ? AND tn.status IN ('bracket', 'knockout')
    """, (team_id,)).fetchall()


@app.route("/admin/spelers/<int:speler_id>/definitief-verwijderen", methods=["POST"])
@login_vereist
def speler_definitief_verwijderen(speler_id):
    """Wis een speler volledig. Kan enkel als hij in geen enkel team meer zit."""
    db = get_db()
    speler = db.execute(f"SELECT p.*, {WEERGAVE} AS naam FROM players p "
                        "WHERE p.id = ?", (speler_id,)).fetchone()
    if not speler:
        flash("Speler niet gevonden.", "fout")
        return redirect(url_for("admin_spelers"))
    if not _bevestigd():
        flash(f"Typ “{BEVESTIGINGSWOORD}” in het vakje om het verwijderen te "
              "bevestigen. Er is niets gewist.", "fout")
        return redirect(url_for("admin_spelers"))
    if speler_id == session.get("speler_id"):
        flash("Je kan je eigen account niet verwijderen.", "fout")
        return redirect(url_for("admin_spelers"))
    bezwaar = rol_blokkade(speler)
    if bezwaar:
        flash(bezwaar, "fout")
        return redirect(url_for("admin_spelers"))

    teams = teams_van_speler(db, speler_id)
    if teams:
        namen = ", ".join(f"“{t['name']}”" for t in teams)
        flash(f"“{speler['naam']}” zit nog in {len(teams)} team(s): {namen}. "
              "Verwijder die eerst; dan kan de speler weg.", "fout")
        return redirect(url_for("admin_spelers"))

    wis_avatar(speler["avatar"])
    db.execute("DELETE FROM game_stats WHERE player_id = ?", (speler_id,))
    db.execute("DELETE FROM game_reports WHERE player_id = ?", (speler_id,))
    # Ook zijn regels uit het claimlogboek: die zouden anders naar een profiel
    # verwijzen dat niet meer bestaat.
    db.execute("DELETE FROM claim_log WHERE player_id = ?", (speler_id,))
    db.execute("DELETE FROM rating_history WHERE entity_type = 'speler' "
               "AND entity_id = ?", (speler_id,))
    db.execute("DELETE FROM season_ratings WHERE entity_type = 'speler' "
               "AND entity_id = ?", (speler_id,))
    db.execute("DELETE FROM players WHERE id = ?", (speler_id,))
    db.commit()
    na_resultaat(db)
    flash(f"Speler “{speler['naam']}” (#{speler_id}) is definitief verwijderd.", "ok")
    return redirect(url_for("admin_spelers"))


@app.route("/admin/teams/<int:team_id>/definitief-verwijderen", methods=["POST"])
@login_vereist
def team_definitief_verwijderen(team_id):
    """Wis een team volledig, inclusief al zijn wedstrijden.

    Alle ratings worden daarna opnieuw berekend alsof die wedstrijden nooit
    gespeeld zijn: wie ooit ELO verloor tegen dit team, krijgt die terug.
    """
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        flash("Team niet gevonden.", "fout")
        return redirect(url_for("admin_spelers") + "#teams")
    if not _bevestigd():
        flash(f"Typ “{BEVESTIGINGSWOORD}” in het vakje om het verwijderen te "
              "bevestigen. Er is niets gewist.", "fout")
        return redirect(url_for("admin_spelers") + "#teams")

    bezig = team_blokkerende_toernooien(db, team_id)
    if bezig:
        namen = ", ".join(f"“{t['name']}”" for t in bezig)
        flash(f"“{team['name']}” speelt nog mee in {namen}. Rond dat toernooi eerst "
              "af of verwijder het, anders klopt het schema niet meer.", "fout")
        return redirect(url_for("admin_spelers") + "#teams")

    aantal = db.execute("SELECT COUNT(*) AS n FROM games WHERE team1_id = ? "
                        "OR team2_id = ?", (team_id, team_id)).fetchone()["n"]

    wis_avatar(team["avatar"])
    # De wedstrijden verdwijnen mee; game_stats, meldingen en ratinghistoriek
    # hangen er met ON DELETE CASCADE aan vast.
    db.execute("DELETE FROM games WHERE team1_id = ? OR team2_id = ?",
               (team_id, team_id))
    db.execute("DELETE FROM season_ratings WHERE entity_type = 'team' "
               "AND entity_id = ?", (team_id,))
    db.execute("DELETE FROM game_reports WHERE team_id = ?", (team_id,))
    db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    db.commit()
    na_resultaat(db)
    flash(f"Team “{team['name']}” is definitief verwijderd, samen met "
          f"{aantal} wedstrijd(en). Alle ELO-ratings zijn herberekend alsof die "
          "wedstrijden nooit gespeeld zijn.", "ok")
    return redirect(url_for("admin_spelers") + "#teams")


@app.route("/admin/spelers/<int:speler_id>/wachtwoord", methods=["POST"])
@login_vereist
def speler_wachtwoord_reset(speler_id):
    db = get_db()
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    nieuw = request.form.get("nieuw", "")
    if not speler:
        flash("Speler niet gevonden.", "fout")
    elif rol_blokkade(speler):
        # Anders kon een organisator het account van een collega overnemen.
        flash(rol_blokkade(speler), "fout")
    elif len(nieuw) < 6:
        flash("Kies een wachtwoord van minstens 6 tekens.", "fout")
    else:
        db.execute("UPDATE players SET password_hash = ? WHERE id = ?",
                   (generate_password_hash(nieuw), speler_id))
        db.commit()
        flash(f"Het wachtwoord van “{weergave(speler)}” is opnieuw ingesteld.", "ok")
    return redirect(url_for("admin_spelers"))


@app.route("/admin/spelers/<int:speler_id>/naam", methods=["POST"])
@login_vereist
def speler_naam_corrigeren(speler_id):
    """Een typfout in de échte naam rechtzetten.

    Spelers kunnen hun eigen naam nooit wijzigen — die hoort bij hun account.
    Een organisator kan wel een verschrijving verbeteren.
    """
    db = get_db()
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    naam = " ".join((request.form.get("naam") or "").split())
    if not speler:
        flash("Speler niet gevonden.", "fout")
    elif rol_blokkade(speler):
        flash(rol_blokkade(speler), "fout")
    elif len(naam) < 3 or len(naam) > 40:
        flash("Een naam telt 3 tot 40 tekens.", "fout")
    elif naam == speler["name"]:
        flash("Dat is al de huidige naam.", "fout")
    else:
        try:
            db.execute("UPDATE players SET name = ? WHERE id = ?", (naam, speler_id))
            db.commit()
            flash(f"De naam van #{speler_id} is gecorrigeerd naar “{naam}”. "
                  "Let op: die naam is ook zijn login.", "ok")
        except Exception:
            db.rollback()
            flash(f"Er bestaat al een account met de naam “{naam}”.", "fout")
    return redirect(url_for("admin_spelers") + "#spelers")


@app.route("/admin/spelers/<int:speler_id>/avatar/wissen", methods=["POST"])
@login_vereist
def speler_avatar_wissen(speler_id):
    db = get_db()
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    if not speler:
        flash("Speler niet gevonden.", "fout")
    elif rol_blokkade(speler):
        flash(rol_blokkade(speler), "fout")
    else:
        wis_avatar(speler["avatar"])
        db.execute("UPDATE players SET avatar = NULL WHERE id = ?", (speler_id,))
        db.commit()
        flash(f"De profielfoto van “{speler['name']}” is verwijderd.", "ok")
    return redirect(url_for("admin_spelers") + "#spelers")


@app.route("/admin/teams/<int:team_id>/avatar/wissen", methods=["POST"])
@login_vereist
def team_avatar_wissen(team_id):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        flash("Team niet gevonden.", "fout")
    else:
        wis_avatar(team["avatar"])
        db.execute("UPDATE teams SET avatar = NULL WHERE id = ?", (team_id,))
        db.commit()
        flash(f"De teamfoto van “{team['name']}” is verwijderd.", "ok")
    return redirect(url_for("admin_spelers") + "#teams")


@app.route("/admin/wedstrijden/nieuw", methods=["POST"])
@login_vereist
def wedstrijd_nieuw():
    db = get_db()
    try:
        team1_id = int(request.form.get("team1", ""))
        team2_id = int(request.form.get("team2", ""))
        speeldag_id = int(request.form.get("speeldag", ""))
    except ValueError:
        flash("Kies twee teams en een speeldag.", "fout")
        return redirect(url_for("admin_wedstrijden"))
    if team1_id == team2_id:
        flash("Kies twee verschillende teams.", "fout")
        return redirect(url_for("admin_wedstrijden"))

    team1 = db.execute("SELECT * FROM teams WHERE id = ? AND status = 'actief'",
                       (team1_id,)).fetchone()
    team2 = db.execute("SELECT * FROM teams WHERE id = ? AND status = 'actief'",
                       (team2_id,)).fetchone()
    speeldag = db.execute("SELECT * FROM matchdays WHERE id = ?",
                          (speeldag_id,)).fetchone()
    if not team1 or not team2 or not speeldag:
        flash("Kies twee actieve teams en een bestaande speeldag.", "fout")
        return redirect(url_for("admin_wedstrijden"))

    spelers = {team1["player1_id"], team1["player2_id"],
               team2["player1_id"], team2["player2_id"]}
    if len(spelers) != 4:
        flash("Deze teams delen een speler en kunnen niet tegen elkaar spelen.", "fout")
        return redirect(url_for("admin_wedstrijden"))

    moment = request.form.get("moment") or f'{speeldag["date"]}T20:00'
    db.execute("INSERT INTO games (team1_id, team2_id, matchday_id, scheduled_at) "
               "VALUES (?, ?, ?, ?)", (team1_id, team2_id, speeldag_id, moment))
    db.commit()
    flash("Wedstrijd ingepland.", "ok")
    return redirect(url_for("admin_wedstrijden") + "#openstaand")


@app.route("/admin/wedstrijden/<int:game_id>/resultaat", methods=["POST"])
@login_vereist
def wedstrijd_resultaat(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game or game["status"] != "gepland":
        flash("Deze wedstrijd bestaat niet of heeft al een resultaat.", "fout")
        return redirect(url_for("admin_wedstrijden"))

    winnaar = request.form.get("winnaar")
    if winnaar not in ("1", "2"):
        flash("Duid de winnaar aan.", "fout")
        return redirect(url_for("admin_wedstrijden") + "#openstaand")
    winnaar_team = game["team1_id"] if winnaar == "1" else game["team2_id"]
    gespeeld_op = request.form.get("gespeeld_op") or datetime.now().strftime("%Y-%m-%dT%H:%M")

    db.execute("UPDATE games SET status = 'gespeeld', winner_team_id = ?, played_at = ? "
               "WHERE id = ?", (winnaar_team, gespeeld_op, game_id))

    for veld, waarde in request.form.items():
        if not veld.startswith("stat_") or not waarde.strip():
            continue
        try:
            _, stat_id, speler_id = veld.split("_")
            db.execute("INSERT OR REPLACE INTO game_stats (game_id, player_id, "
                       "stat_type_id, value) VALUES (?, ?, ?, ?)",
                       (game_id, int(speler_id), int(stat_id), float(waarde.replace(",", "."))))
        except ValueError:
            continue

    db.commit()
    for melding in na_resultaat(db, game_id):
        flash(melding, "ok")
    flash("Resultaat opgeslagen — de ELO-ratings en klassementen zijn bijgewerkt.", "ok")
    if game["tournament_id"]:
        return redirect(url_for("toernooi_beheer", toernooi_id=game["tournament_id"])
                        + "#openstaand")
    return redirect(url_for("admin_wedstrijden") + "#openstaand")


@app.route("/admin/wedstrijden/<int:game_id>/verwijderen", methods=["POST"])
@login_vereist
def wedstrijd_verwijderen(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        flash("Wedstrijd niet gevonden.", "fout")
        return redirect(url_for("admin_wedstrijden"))
    was_gespeeld = game["status"] == "gespeeld"
    toernooi_id = game["tournament_id"]

    if toernooi_id:
        # Toernooiwedstrijden horen bij het schema: we verwijderen ze nooit, we
        # wissen enkel de uitslag zodat ze opnieuw gespeeld kunnen worden.
        terug = url_for("toernooi_beheer", toernooi_id=toernooi_id) + "#openstaand"
        if not was_gespeeld:
            flash("Deze wedstrijd hoort bij het toernooischema en kan niet los "
                  "verwijderd worden. Gebruik “Loting ongedaan maken” als je het "
                  "hele programma opnieuw wil trekken.", "fout")
            return redirect(terug)

        reden = toernooi_motor.mag_wissen(db, game)
        if reden:
            flash(reden, "fout")
            return redirect(terug)

        db.execute("UPDATE games SET status = 'gepland', winner_team_id = NULL, "
                   "played_at = NULL WHERE id = ?", (game_id,))
        db.execute("DELETE FROM game_reports WHERE game_id = ?", (game_id,))
        db.commit()
        # Ruim op wat op deze uitslag voortbouwde (doorgeschoven winnaar, of het
        # hele knockoutschema bij een bracket- of shootoutuitslag).
        toernooi_motor.herstel_na_wissen(db, toernooi_id, game["fase"], game)
        for melding in na_resultaat(db):
            flash(melding, "ok")
        flash("Het resultaat is gewist; de wedstrijd staat weer open." +
              (" Het knockoutschema wordt opnieuw bepaald zodra de stand vastligt."
               if game["fase"] in ("bracket", "shootout") else ""), "ok")
        return redirect(terug)

    db.execute("DELETE FROM games WHERE id = ?", (game_id,))
    db.commit()
    if was_gespeeld:
        na_resultaat(db)
        flash("Wedstrijd verwijderd en alle ratings herberekend.", "ok")
    else:
        flash("Geplande wedstrijd verwijderd.", "ok")
    return redirect(url_for("admin_wedstrijden"))


@app.route("/admin/stats/nieuw", methods=["POST"])
@login_vereist
def stat_nieuw():
    naam = (request.form.get("naam") or "").strip()
    eenheid = (request.form.get("eenheid") or "").strip()
    if not naam:
        flash("Geef de statistiek een naam.", "fout")
        return redirect(url_for("admin_klassement") + "#statistieken")
    db = get_db()
    try:
        db.execute("INSERT INTO stat_types (name, unit) VALUES (?, ?)", (naam, eenheid))
        db.commit()
        flash(f"Statistiek “{naam}” aangemaakt. Ze verschijnt vanaf nu op elk "
              "resultaatformulier en op de statistiekenpagina.", "ok")
    except Exception:
        flash(f"Er bestaat al een statistiek met de naam “{naam}”.", "fout")
    return redirect(url_for("admin_klassement") + "#statistieken")


@app.route("/admin/stats/<int:stat_id>/toggle", methods=["POST"])
@login_vereist
def stat_toggle(stat_id):
    db = get_db()
    db.execute("UPDATE stat_types SET active = 1 - active WHERE id = ?", (stat_id,))
    db.commit()
    flash("Statistiek bijgewerkt.", "ok")
    return redirect(url_for("admin_klassement") + "#statistieken")


@app.route("/admin/rangen/nieuw", methods=["POST"])
@login_vereist
def rang_nieuw():
    naam = (request.form.get("naam") or "").strip()
    kleur = request.form.get("kleur") or "#64748b"
    try:
        min_elo = float(request.form.get("min_elo", ""))
        max_elo = float(request.form.get("max_elo", ""))
    except ValueError:
        flash("Vul geldige ELO-grenzen in.", "fout")
        return redirect(url_for("admin_klassement") + "#rangen")
    if not naam or min_elo >= max_elo:
        flash("Geef een naam op en zorg dat de ondergrens lager is dan de bovengrens.", "fout")
        return redirect(url_for("admin_klassement") + "#rangen")
    db = get_db()
    try:
        db.execute("INSERT INTO ranks (name, min_elo, max_elo, color) VALUES (?, ?, ?, ?)",
                   (naam, min_elo, max_elo, kleur))
        db.commit()
        flash(f"Rang “{naam}” toegevoegd ({min_elo:.0f}–{max_elo:.0f} ELO).", "ok")
    except Exception:
        flash(f"Er bestaat al een rang met de naam “{naam}”.", "fout")
    return redirect(url_for("admin_klassement") + "#rangen")


@app.route("/admin/rangen/<int:rang_id>/verwijderen", methods=["POST"])
@login_vereist
def rang_verwijderen(rang_id):
    db = get_db()
    db.execute("DELETE FROM ranks WHERE id = ?", (rang_id,))
    db.commit()
    flash("Rang verwijderd.", "ok")
    return redirect(url_for("admin_klassement") + "#rangen")


@app.route("/admin/instellingen", methods=["POST"])
@login_vereist
@eigenaar_vereist
def instellingen_opslaan():
    db = get_db()
    try:
        k_s = float(request.form.get("k_speler", "32"))
        k_t = float(request.form.get("k_team", "32"))
        if not (1 <= k_s <= 200 and 1 <= k_t <= 200):
            raise ValueError
    except ValueError:
        flash("K-factoren moeten getallen tussen 1 en 200 zijn.", "fout")
        return redirect(url_for("admin_instellingen"))
    zet_instelling(db, "k_speler", k_s)
    zet_instelling(db, "k_team", k_t)
    db.commit()
    herbereken_alles(db)
    flash("K-factoren opgeslagen en alle ratings herberekend.", "ok")
    return redirect(url_for("admin_instellingen"))


# ----------------------------------------------------------------- back-up --
#
# De hele competitie zit in één bestand (shuss.db). Een kopie maken kan dus
# altijd — maar níét met een gewone bestandskopie: als er op dat moment
# geschreven wordt, krijg je een halve database. De backup-API van SQLite maakt
# wél een samenhangende kopie, ook terwijl de site draait.

BACKUPS_BEWAREN = 20


def maak_backup(reden="handmatig"):
    """Schrijf een samenhangende kopie van de database naar backups/."""
    os.makedirs(BACKUP_MAP, exist_ok=True)
    naam = f"shuss_{datetime.now():%Y%m%d_%H%M%S}_{reden}.db"
    pad = os.path.join(BACKUP_MAP, naam)
    bron = verbind(DB_PATH)
    doel = sqlite3.connect(pad)
    with doel:
        bron.backup(doel)
    doel.close()
    bron.close()
    _ruim_backups_op()
    return pad


def _ruim_backups_op():
    """Hou enkel de nieuwste back-ups bij, zodat de map niet blijft groeien."""
    if not os.path.isdir(BACKUP_MAP):
        return
    bestanden = sorted(
        (os.path.join(BACKUP_MAP, n) for n in os.listdir(BACKUP_MAP)
         if n.startswith("shuss_") and n.endswith(".db")),
        key=os.path.getmtime, reverse=True)
    for oud in bestanden[BACKUPS_BEWAREN:]:
        try:
            os.remove(oud)
        except OSError:
            pass


def backup_lijst():
    if not os.path.isdir(BACKUP_MAP):
        return []
    uit = []
    for naam in os.listdir(BACKUP_MAP):
        if naam.startswith("shuss_") and naam.endswith(".db"):
            pad = os.path.join(BACKUP_MAP, naam)
            uit.append({"naam": naam, "grootte": os.path.getsize(pad),
                        "moment": datetime.fromtimestamp(os.path.getmtime(pad))})
    return sorted(uit, key=lambda b: b["moment"], reverse=True)


@app.route("/admin/backup/nu", methods=["POST"])
@login_vereist
@eigenaar_vereist
def backup_nu():
    try:
        pad = maak_backup("handmatig")
        flash(f"Back-up gemaakt: {os.path.basename(pad)}. Ze staat in de map "
              "“backups” naast de database.", "ok")
    except Exception as fout:
        flash(f"De back-up is niet gelukt: {fout}", "fout")
    return redirect(url_for("admin_instellingen") + "#backup")


@app.route("/admin/backup/download")
@login_vereist
@eigenaar_vereist
def backup_download():
    """Een verse kopie van de database rechtstreeks downloaden."""
    pad = maak_backup("download")
    return send_from_directory(BACKUP_MAP, os.path.basename(pad),
                               as_attachment=True,
                               download_name=f"shuss_{date.today():%Y-%m-%d}.db")


@app.route("/admin/league", methods=["POST"])
@login_vereist
@eigenaar_vereist
def league_schakelen():
    """Het leaguegedeelte tonen of verbergen voor de spelers."""
    db = get_db()
    aan = request.form.get("league_actief") == "1"
    zet_instelling(db, "league_actief", "1" if aan else "0")
    db.commit()
    if aan:
        flash("Het leaguegedeelte staat aan: iedereen ziet nu het klassement, de "
              "wedstrijden, de statistieken en de seizoenen.", "ok")
    else:
        flash("Het leaguegedeelte staat uit: spelers zien enkel nog het toernooi. "
              "Jij houdt als organisator toegang tot de leaguepagina's.", "ok")
    return redirect(url_for("admin_instellingen") + "#league")


@app.route("/admin/claimvenster", methods=["POST"])
@login_vereist
def claimvenster_schakelen():
    """Het opeisen van accounts open- of dichtzetten."""
    db = get_db()
    aan = request.form.get("claim_open") == "1"
    zet_instelling(db, "claim_open", "1" if aan else "0")
    db.commit()
    if aan:
        vrij = len(vrije_accounts(db))
        flash(f"Accounts opeisen staat open: {vrij} account(s) wachten nog op een "
              "wachtwoord. Zet het weer dicht zodra iedereen binnen is.", "ok")
    else:
        flash("Accounts opeisen staat weer dicht.", "ok")
    return redirect(url_for("admin_spelers") + "#claimen")


@app.route("/admin/claimen/scherm")
@login_vereist
def claim_scherm():
    """Groot scherm voor op de beamer: QR-code naar de opeispagina.

    Ververst zichzelf, zodat je live ziet hoeveel accounts er nog vrij zijn.
    """
    db = get_db()
    adres = externe_url("claimen")
    vrij = vrije_accounts(db)
    geclaimd = db.execute("SELECT COUNT(*) AS n FROM players "
                          "WHERE password_hash IS NOT NULL").fetchone()["n"]
    return render_template("claim_scherm.html", adres=adres, qr=qr_svg(adres),
                           vrij=vrij, geclaimd=geclaimd, open=claim_open())


@app.route("/admin/spelers/<int:speler_id>/vrijgeven", methods=["POST"])
@login_vereist
def speler_vrijgeven(speler_id):
    """Het wachtwoord wissen zodat het account opnieuw opgeëist kan worden.

    Handig als iemand per ongeluk (of ten onrechte) het verkeerde account nam.
    """
    db = get_db()
    speler = db.execute("SELECT * FROM players WHERE id = ?", (speler_id,)).fetchone()
    if not speler:
        flash("Speler niet gevonden.", "fout")
    elif rol_blokkade(speler):
        flash(rol_blokkade(speler), "fout")
    elif not speler["password_hash"]:
        flash(f"“{weergave(speler)}” heeft nog geen wachtwoord.", "fout")
    else:
        db.execute("UPDATE players SET password_hash = NULL WHERE id = ?", (speler_id,))
        _log_claim(db, speler, "vrijgegeven")
        db.commit()
        flash(f"Het wachtwoord van “{weergave(speler)}” is gewist. Het account staat "
              "weer vrij om opgeëist te worden.", "ok")
    return redirect(url_for("admin_spelers") + "#claimen")


# --------------------------------------------- database legen en terugzetten --
#
# Twee zware ingrepen, allebei enkel voor de eigenaar en allebei met een woord
# dat je zelf moet intypen. Vóór elke ingreep gaat er automatisch een back-up
# naar de map backups/, zodat een vergissing nooit definitief is.

# Tabellen die enkel over gespeelde geschiedenis gaan (spelers en teams blijven).
GESCHIEDENIS_TABELLEN = ["game_stats", "game_reports", "rating_history",
                         "season_ratings", "games", "tournament_teams",
                         "tournament_locations", "tournaments", "matchdays",
                         "seasons"]
# Daarbovenop bij "alles wissen":
ALLES_TABELLEN = GESCHIEDENIS_TABELLEN + ["claim_log", "teams", "players"]


def _woord_klopt(verwacht):
    return " ".join((request.form.get("bevestiging") or "").split()).lower() == verwacht


@app.route("/admin/database/legen", methods=["POST"])
@login_vereist
@eigenaar_vereist
def database_legen():
    db = get_db()
    alles = request.form.get("omvang") == "alles"
    woord = "wis alles" if alles else "wis geschiedenis"
    if not _woord_klopt(woord):
        flash(f"Typ “{woord}” in het vakje om te bevestigen. Er is niets gewist.", "fout")
        return redirect(url_for("admin_instellingen") + "#leegmaken")

    try:
        kopie = maak_backup("voor-legen")
    except Exception as fout:
        flash(f"De veiligheidsback-up lukte niet ({fout}); er is niets gewist.", "fout")
        return redirect(url_for("admin_instellingen") + "#leegmaken")

    tabellen = ALLES_TABELLEN if alles else GESCHIEDENIS_TABELLEN
    db.execute("PRAGMA foreign_keys = OFF")
    for tabel in tabellen:
        db.execute(f"DELETE FROM {tabel}")
    if not alles:
        # Spelers en teams blijven, maar beginnen weer van nul.
        db.execute("UPDATE players SET elo = ?", (START_ELO,))
        db.execute("UPDATE teams SET elo = ?", (START_ELO,))
    db.execute("DELETE FROM sqlite_sequence")
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
    for map_ in (UPLOAD_MAP,) if alles else ():
        for naam in os.listdir(map_) if os.path.isdir(map_) else []:
            if _AVATAR_RE.match(naam):
                try:
                    os.remove(os.path.join(map_, naam))
                except OSError:
                    pass
    if alles:
        db.execute("UPDATE settings SET value = '0' WHERE key = 'claim_open'")
        db.commit()
        session.clear()
        flash("De database is volledig leeggemaakt. Maak nu een nieuw account aan — "
              "het eerste account wordt automatisch de eigenaar. Een back-up van de "
              f"oude stand staat in backups/{os.path.basename(kopie)}.", "ok")
        return redirect(url_for("registreren"))

    flash("Alle wedstrijden, toernooien en seizoenen zijn gewist; spelers en teams "
          f"blijven en staan weer op {START_ELO:.0f} ELO. Een back-up van de oude "
          f"stand staat in backups/{os.path.basename(kopie)}.", "ok")
    return redirect(url_for("admin_instellingen") + "#leegmaken")


def _is_geldige_database(pad):
    """Is dit echt een Leberschuss-database? Geeft None terug als ze deugt."""
    try:
        with open(pad, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return "Dit is geen databasebestand (.db) van deze site."
        keur = sqlite3.connect(f"file:{pad}?mode=ro", uri=True)
        if keur.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            keur.close()
            return "Het bestand is beschadigd."
        aanwezig = {r[0] for r in keur.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        keur.close()
        ontbreekt = {"players", "teams", "games", "settings"} - aanwezig
        if ontbreekt:
            return ("Dit lijkt geen Leberschuss-database: "
                    f"{', '.join(sorted(ontbreekt))} ontbreekt.")
    except Exception as fout:
        return f"Het bestand kon niet gelezen worden ({fout})."
    return None


@app.route("/admin/backup/terugzetten", methods=["POST"])
@login_vereist
@eigenaar_vereist
def backup_terugzetten():
    if not _woord_klopt("terugzetten"):
        flash("Typ “terugzetten” in het vakje om te bevestigen. Er is niets "
              "gewijzigd.", "fout")
        return redirect(url_for("admin_instellingen") + "#terugzetten")
    bestand = request.files.get("bestand")
    if not bestand or not bestand.filename:
        flash("Kies eerst een back-upbestand.", "fout")
        return redirect(url_for("admin_instellingen") + "#terugzetten")

    os.makedirs(BACKUP_MAP, exist_ok=True)
    tijdelijk = os.path.join(BACKUP_MAP, f"upload_{secrets.token_hex(4)}.db")
    bestand.save(tijdelijk)

    bezwaar = _is_geldige_database(tijdelijk)
    if bezwaar:
        os.remove(tijdelijk)
        flash(f"{bezwaar} Er is niets gewijzigd.", "fout")
        return redirect(url_for("admin_instellingen") + "#terugzetten")

    try:
        kopie = maak_backup("voor-terugzetten")
    except Exception as fout:
        os.remove(tijdelijk)
        flash(f"De veiligheidsback-up lukte niet ({fout}); er is niets gewijzigd.", "fout")
        return redirect(url_for("admin_instellingen") + "#terugzetten")

    # De verbinding van dit verzoek eerst sluiten, anders houdt ze het oude
    # bestand nog vast terwijl we het vervangen.
    oud = g.pop("db", None)
    if oud is not None:
        oud.close()
    os.replace(tijdelijk, DB_PATH)
    # Resten van de vorige database: die horen niet bij het nieuwe bestand.
    for extra in (DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(extra):
            os.remove(extra)
    init_db(DB_PATH)                      # schema bijwerken als de kopie ouder is

    db = get_db()
    aantal = db.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
    ik = db.execute("SELECT 1 FROM players WHERE id = ?",
                    (session.get("speler_id"),)).fetchone()
    if not ik:
        session.clear()
        flash("De database is vervangen. Jouw account staat niet in dit bestand, "
              "dus je bent uitgelogd. Een back-up van de vorige stand staat in "
              f"backups/{os.path.basename(kopie)}.", "ok")
        return redirect(url_for("inloggen"))
    flash(f"De database is vervangen: {aantal} speler(s) ingeladen. Een back-up van "
          f"de vorige stand staat in backups/{os.path.basename(kopie)}.", "ok")
    return redirect(url_for("admin_instellingen") + "#terugzetten")


@app.route("/mijn-wachtwoord", methods=["POST"])
@speler_vereist
def wachtwoord_wijzigen():
    """Je eigen wachtwoord wijzigen — het huidige wachtwoord is vereist."""
    db = get_db()
    ik = huidige_speler()
    huidig = request.form.get("huidig", "")
    nieuw = request.form.get("nieuw", "")
    terug = redirect(url_for("speler_profiel", speler_id=ik["id"]))
    if not ik["password_hash"] or not check_password_hash(ik["password_hash"], huidig):
        flash("Je huidige wachtwoord klopt niet.", "fout")
    elif len(nieuw) < 6:
        flash("Kies een nieuw wachtwoord van minstens 6 tekens.", "fout")
    else:
        db.execute("UPDATE players SET password_hash = ? WHERE id = ?",
                   (generate_password_hash(nieuw), ik["id"]))
        db.commit()
        flash("Je wachtwoord is gewijzigd.", "ok")
    return terug


@app.route("/admin/herberekenen", methods=["POST"])
@login_vereist
@eigenaar_vereist
def herberekenen():
    db = get_db()
    for melding in na_resultaat(db):
        flash(melding, "ok")
    flash("Alle ratings zijn opnieuw berekend.", "ok")
    return redirect(url_for("admin_instellingen"))


# ---------------------------------------------------------------- toernooi --

def _toernooi_of_404(db, toernooi_id):
    t = toernooi_motor.toernooi(db, toernooi_id)
    if not t:
        abort(404)
    return t


TOERNOOI_STATUS = {
    "opzet": ("In opbouw", "De organisatie stelt de teams nog samen."),
    "bracket": ("Bracketfase", "Alle teams spelen in één grote bracket."),
    "knockout": ("Knockout", "De besten strijden om de beker."),
    "afgelopen": ("Afgelopen", "Dit toernooi zit erop."),
}


@app.route("/toernooien")
def toernooien():
    db = get_db()
    lijst = []
    for t in db.execute("SELECT * FROM tournaments ORDER BY date DESC, id DESC"):
        aantal = db.execute("SELECT COUNT(*) AS n FROM tournament_teams "
                            "WHERE tournament_id = ?", (t["id"],)).fetchone()["n"]
        gespeeld = db.execute("SELECT COUNT(*) AS n FROM games WHERE tournament_id = ? "
                              "AND status = 'gespeeld'", (t["id"],)).fetchone()["n"]
        totaal = db.execute("SELECT COUNT(*) AS n FROM games WHERE tournament_id = ?",
                            (t["id"],)).fetchone()["n"]
        winnaar = None
        if t["status"] == "afgelopen":
            f = db.execute("SELECT winner_team_id FROM games WHERE tournament_id = ? "
                           "AND fase = 'knockout' AND ronde = 2", (t["id"],)).fetchone()
            if f and f["winner_team_id"]:
                winnaar = db.execute("SELECT id, name FROM teams WHERE id = ?",
                                     (f["winner_team_id"],)).fetchone()
        lijst.append({"t": t, "teams": aantal, "gespeeld": gespeeld, "totaal": totaal,
                      "label": TOERNOOI_STATUS.get(t["status"], ("", ""))[0],
                      "uitleg": TOERNOOI_STATUS.get(t["status"], ("", ""))[1],
                      "winnaar": winnaar})
    return render_template("toernooien.html", lijst=lijst)


@app.route("/toernooi/<int:toernooi_id>")
def toernooi_detail(toernooi_id):
    db = get_db()
    t = _toernooi_of_404(db, toernooi_id)
    teams = toernooi_motor.deelnemers(db, toernooi_id)
    groepen, namen, loc_namen = toernooi_motor.kalender(db, toernooi_id)
    kolommen = toernooi_motor.knockout_kolommen(db, toernooi_id)
    rangschikking, beslissend = ([], [])
    if t["status"] != "opzet":
        rangschikking, beslissend = toernooi_motor.stand(db, toernooi_id)

    deltas = {}
    for r in db.execute("""
        SELECT rh.game_id, rh.entity_id, rh.elo_voor, rh.elo_na
        FROM rating_history rh
        JOIN games g ON g.id = rh.game_id
        WHERE rh.entity_type = 'team' AND rh.scope = 'permanent' AND g.tournament_id = ?
    """, (toernooi_id,)):
        deltas[(r["game_id"], r["entity_id"])] = r["elo_na"] - r["elo_voor"]

    open_shootouts = db.execute(
        "SELECT COUNT(*) AS n FROM games WHERE tournament_id = ? AND fase = 'shootout' "
        "AND status = 'gepland'", (toernooi_id,)).fetchone()["n"]

    winnaar = None
    finale = db.execute("SELECT * FROM games WHERE tournament_id = ? AND "
                        "fase = 'knockout' AND ronde = 2", (toernooi_id,)).fetchone()
    if finale and finale["winner_team_id"]:
        winnaar = db.execute("SELECT * FROM teams WHERE id = ?",
                             (finale["winner_team_id"],)).fetchone()

    # Eigen wedstrijden van de ingelogde speler binnen dit toernooi.
    mijn_wedstrijden = []
    if session.get("speler_id"):
        mijn_wedstrijden = speler_wedstrijd_meldingen(db, session["speler_id"],
                                                      toernooi_id=toernooi_id)
    mijn_gespeeld = []
    if session.get("speler_id"):
        mijn_gespeeld = db.execute("""
            SELECT g.*, t1.player1_id AS a1, t1.player2_id AS a2
            FROM games g
            JOIN teams t1 ON t1.id = g.team1_id
            JOIN teams t2 ON t2.id = g.team2_id
            WHERE g.tournament_id = ? AND g.status = 'gespeeld'
              AND ? IN (t1.player1_id, t1.player2_id, t2.player1_id, t2.player2_id)
            ORDER BY g.played_at DESC, g.id DESC
        """, (toernooi_id, session["speler_id"])).fetchall()
    actieve_types = db.execute("SELECT * FROM stat_types WHERE active = 1 "
                               "ORDER BY name").fetchall()

    return render_template("toernooi.html", t=t, huidig_toernooi=t, teams=teams,
                           groepen=groepen,
                           mijn_wedstrijden=mijn_wedstrijden,
                           mijn_gespeeld=mijn_gespeeld,
                           actieve_types=actieve_types,
                           namen=namen, loc_namen=loc_namen, kolommen=kolommen,
                           stand=rangschikking, beslissend=beslissend, deltas=deltas,
                           tiebreaks=(toernooi_motor.tiebreak_groepen(db, toernooi_id)
                                      if t["status"] != "opzet" else []),
                           winnaar=winnaar, open_shootouts=open_shootouts,
                           label=TOERNOOI_STATUS.get(t["status"], ("", ""))[0],
                           heeft_loting=t["status"] != "opzet")


@app.route("/toernooi/<int:toernooi_id>/loting")
def toernooi_loting(toernooi_id):
    db = get_db()
    t = _toernooi_of_404(db, toernooi_id)
    if t["status"] == "opzet":
        flash("Dit toernooi is nog niet geloot.", "fout")
        return redirect(url_for("toernooi_detail", toernooi_id=toernooi_id))
    potten, rondes = toernooi_motor.loting_data(db, toernooi_id)
    return render_template("toernooi_loting.html", t=t, huidig_toernooi=t,
                           potten=potten, rondes=rondes)


# ---------------------------------------------------------- toernooi-admin --

@app.route("/admin/toernooien/nieuw", methods=["POST"])
@login_vereist
def toernooi_nieuw():
    db = get_db()
    naam = (request.form.get("naam") or "").strip()
    datum = request.form.get("datum") or ""
    if not naam or not datum:
        flash("Geef het toernooi een naam en een datum.", "fout")
        return redirect(url_for("admin_toernooien"))
    try:
        db.execute("""
            INSERT INTO tournaments (name, description, date, start_tijd,
                                     bracket_ronden, ko_teams, potten, slot_minuten)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (naam, (request.form.get("beschrijving") or "").strip(), datum,
              request.form.get("start_tijd") or "19:00",
              int(request.form.get("ronden") or 4),
              int(request.form.get("ko_teams") or 8),
              int(request.form.get("potten") or 4),
              int(request.form.get("slot") or 20)))
        db.commit()
        nieuw = db.execute("SELECT id FROM tournaments WHERE name = ?", (naam,)).fetchone()
        flash(f"Toernooi “{naam}” aangemaakt. Voeg nu teams en tafels toe.", "ok")
        return redirect(url_for("toernooi_beheer", toernooi_id=nieuw["id"]))
    except ValueError:
        flash("Vul geldige getallen in.", "fout")
    except Exception:
        flash(f"Er bestaat al een toernooi met de naam “{naam}”.", "fout")
    return redirect(url_for("admin_toernooien"))


@app.route("/admin/toernooi/<int:toernooi_id>")
@login_vereist
def toernooi_beheer(toernooi_id):
    db = get_db()
    t = _toernooi_of_404(db, toernooi_id)
    teams = toernooi_motor.deelnemers(db, toernooi_id)
    gekozen = {x["id"] for x in teams}

    namen_sp = weergavenamen(db)
    # Spelers die al meedoen: hun andere teams kunnen er niet meer bij, want
    # niemand kan aan twee tafels tegelijk staan.
    bezet = {}
    for x in teams:
        bezet[x["player1_id"]] = x["naam"]
        bezet[x["player2_id"]] = x["naam"]

    beschikbaar = []
    for r in db.execute("SELECT * FROM teams WHERE status = 'actief' ORDER BY elo DESC"):
        if r["id"] in gekozen:
            continue
        botsing = next((pid for pid in (r["player1_id"], r["player2_id"])
                        if pid in bezet), None)
        beschikbaar.append({"id": r["id"], "naam": r["name"], "elo": r["elo"],
                            "spelers": f'{namen_sp.get(r["player1_id"], "?")} & '
                                       f'{namen_sp.get(r["player2_id"], "?")}',
                            "botsing": (f"{namen_sp.get(botsing, 'een speler')} speelt "
                                        f"al voor “{bezet[botsing]}”")
                                       if botsing is not None else None})

    openstaand = db.execute("""
        SELECT * FROM games WHERE tournament_id = ? AND status = 'gepland'
          AND team1_id IS NOT NULL AND team2_id IS NOT NULL
        ORDER BY scheduled_at, id
    """, (toernooi_id,)).fetchall()
    recent = db.execute("""
        SELECT * FROM games WHERE tournament_id = ? AND status = 'gespeeld'
        ORDER BY played_at DESC, id DESC LIMIT 12
    """, (toernooi_id,)).fetchall()

    meldingen_per_game = defaultdict(list)
    for r in db.execute(f"""
        SELECT gr.*, {WEERGAVE} AS speler_naam
        FROM game_reports gr
        JOIN players p ON p.id = gr.player_id
        JOIN games g ON g.id = gr.game_id
        WHERE g.tournament_id = ?
        ORDER BY gr.created_at
    """, (toernooi_id,)):
        meldingen_per_game[r["game_id"]].append(r)

    return render_template("admin_toernooi.html", t=t, teams=teams,
                           beschikbaar=beschikbaar,
                           locaties=toernooi_motor.locaties(db, toernooi_id),
                           openstaand=openstaand, recent=recent,
                           namen=teamnamen(db),
                           meldingen_per_game=meldingen_per_game,
                           inzet=toernooi_motor.shootout_inzet(db, toernooi_id),
                           controle=toernooi_motor.controleer(db, toernooi_id),
                           nu=datetime.now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/admin/toernooi/<int:toernooi_id>/instellingen", methods=["POST"])
@login_vereist
def toernooi_instellingen(toernooi_id):
    db = get_db()
    t = _toernooi_of_404(db, toernooi_id)
    try:
        velden = {
            "name": (request.form.get("naam") or t["name"]).strip(),
            "description": (request.form.get("beschrijving") or "").strip(),
            "date": request.form.get("datum") or t["date"],
            "start_tijd": request.form.get("start_tijd") or t["start_tijd"],
            "bracket_ronden": int(request.form.get("ronden") or t["bracket_ronden"]),
            "ko_teams": int(request.form.get("ko_teams") or t["ko_teams"]),
            "potten": int(request.form.get("potten") or t["potten"]),
            "slot_minuten": int(request.form.get("slot") or t["slot_minuten"]),
        }
    except ValueError:
        flash("Vul geldige getallen in.", "fout")
        return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))
    if t["status"] != "opzet":
        # Na de loting mogen enkel de tekstvelden nog wijzigen.
        velden = {k: velden[k] for k in ("name", "description")}
    db.execute(f"UPDATE tournaments SET {', '.join(f'{k} = ?' for k in velden)} "
               "WHERE id = ?", (*velden.values(), toernooi_id))
    db.commit()
    flash("Toernooi bijgewerkt.", "ok")
    return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))


@app.route("/admin/toernooi/<int:toernooi_id>/teams", methods=["POST"])
@login_vereist
def toernooi_teams(toernooi_id):
    db = get_db()
    t = _toernooi_of_404(db, toernooi_id)
    if t["status"] != "opzet":
        flash("Het toernooi is al geloot; je kan de deelnemers niet meer wijzigen.", "fout")
        return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))

    # Wie doet er al mee? Een speler kan niet in twee teams tegelijk spelen —
    # hij zou dan op hetzelfde moment aan twee tafels moeten staan.
    bezet = {}
    for r in db.execute("""
        SELECT t.name, t.player1_id, t.player2_id
        FROM tournament_teams tt JOIN teams t ON t.id = tt.team_id
        WHERE tt.tournament_id = ?
    """, (toernooi_id,)):
        bezet[r["player1_id"]] = r["name"]
        bezet[r["player2_id"]] = r["name"]
    namen_sp = weergavenamen(db)

    toegevoegd, geweigerd = 0, []
    for waarde in request.form.getlist("team"):
        try:
            team_id = int(waarde)
        except ValueError:
            continue
        team = db.execute("SELECT * FROM teams WHERE id = ? AND status = 'actief'",
                          (team_id,)).fetchone()
        if not team:
            continue
        botsing = next((pid for pid in (team["player1_id"], team["player2_id"])
                        if pid in bezet), None)
        if botsing is not None:
            geweigerd.append(f"“{team['name']}” niet: {namen_sp.get(botsing, 'een speler')} "
                             f"speelt al voor “{bezet[botsing]}”")
            continue
        try:
            db.execute("INSERT INTO tournament_teams (tournament_id, team_id) "
                       "VALUES (?, ?)", (toernooi_id, team_id))
            toegevoegd += 1
            bezet[team["player1_id"]] = team["name"]
            bezet[team["player2_id"]] = team["name"]
        except Exception:
            pass
    db.commit()
    if toegevoegd:
        flash(f"{toegevoegd} team(s) toegevoegd aan het toernooi.", "ok")
    if geweigerd:
        flash("Elke speler kan maar voor één team per toernooi spelen. "
              + "; ".join(geweigerd) + ".", "fout")
    elif not toegevoegd:
        flash("Er zijn geen teams toegevoegd.", "fout")
    return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))


@app.route("/admin/toernooi/<int:toernooi_id>/teams/<int:team_id>/weg", methods=["POST"])
@login_vereist
def toernooi_team_weg(toernooi_id, team_id):
    db = get_db()
    t = _toernooi_of_404(db, toernooi_id)
    if t["status"] != "opzet":
        flash("Het toernooi is al geloot; je kan de deelnemers niet meer wijzigen.", "fout")
    else:
        db.execute("DELETE FROM tournament_teams WHERE tournament_id = ? AND team_id = ?",
                   (toernooi_id, team_id))
        db.commit()
        flash("Team uit het toernooi gehaald.", "ok")
    return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))


@app.route("/admin/toernooi/<int:toernooi_id>/locaties", methods=["POST"])
@login_vereist
def toernooi_locatie_nieuw(toernooi_id):
    db = get_db()
    _toernooi_of_404(db, toernooi_id)
    naam = (request.form.get("naam") or "").strip()
    if not naam:
        flash("Geef de locatie een naam, bv. “Tafel 1”.", "fout")
        return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))
    try:
        db.execute("INSERT INTO tournament_locations (tournament_id, name) VALUES (?, ?)",
                   (toernooi_id, naam))
        db.commit()
        flash(f"Locatie “{naam}” toegevoegd.", "ok")
    except Exception:
        flash(f"“{naam}” bestaat al voor dit toernooi.", "fout")
    return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))


@app.route("/admin/toernooi/<int:toernooi_id>/locaties/<int:locatie_id>/weg",
           methods=["POST"])
@login_vereist
def toernooi_locatie_weg(toernooi_id, locatie_id):
    db = get_db()
    db.execute("DELETE FROM tournament_locations WHERE id = ? AND tournament_id = ?",
               (locatie_id, toernooi_id))
    db.commit()
    flash("Locatie verwijderd.", "ok")
    return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))


@app.route("/admin/toernooi/<int:toernooi_id>/genereren", methods=["POST"])
@login_vereist
def toernooi_genereren(toernooi_id):
    db = get_db()
    _toernooi_of_404(db, toernooi_id)
    ok, boodschap = toernooi_motor.genereer(db, toernooi_id)
    flash(boodschap, "ok" if ok else "fout")
    if ok:
        return redirect(url_for("toernooi_loting", toernooi_id=toernooi_id))
    return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))


@app.route("/admin/toernooi/<int:toernooi_id>/bijwerken", methods=["POST"])
@login_vereist
def toernooi_bijwerken(toernooi_id):
    """Stand, shootouts en knockoutschema opnieuw laten bepalen."""
    db = get_db()
    _toernooi_of_404(db, toernooi_id)
    meldingen = toernooi_motor.evalueer(db, toernooi_id)
    for melding in meldingen:
        flash(melding, "ok")
    if not meldingen:
        flash("De stand is bijgewerkt; er viel niets te wijzigen.", "ok")
    return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))


@app.route("/admin/toernooi/<int:toernooi_id>/herloten", methods=["POST"])
@login_vereist
def toernooi_herloten(toernooi_id):
    db = get_db()
    _toernooi_of_404(db, toernooi_id)
    ok, boodschap = toernooi_motor.herloot(db, toernooi_id)
    flash(boodschap, "ok" if ok else "fout")
    return redirect(url_for("toernooi_beheer", toernooi_id=toernooi_id))


@app.route("/admin/toernooi/<int:toernooi_id>/verwijderen", methods=["POST"])
@login_vereist
def toernooi_verwijderen(toernooi_id):
    db = get_db()
    t = _toernooi_of_404(db, toernooi_id)
    gespeeld = db.execute("SELECT COUNT(*) AS n FROM games WHERE tournament_id = ? "
                          "AND status = 'gespeeld'", (toernooi_id,)).fetchone()["n"]
    db.execute("DELETE FROM tournaments WHERE id = ?", (toernooi_id,))
    db.commit()
    if gespeeld:
        na_resultaat(db)
    flash(f"Toernooi “{t['name']}” verwijderd." +
          (" Alle ratings zijn herberekend." if gespeeld else ""), "ok")
    return redirect(url_for("admin_toernooien"))


# -------------------------------------------------------------------- main --

init_db()
os.makedirs(UPLOAD_MAP, exist_ok=True)


def _backup_bij_opstarten(minstens_uren=1):
    """Bij het opstarten een kopie maken: zo heb je altijd een recent vangnet.

    Draait de site achter een webserver met meerdere processen, dan start deze
    code meermaals; en tijdens het sleutelen herstart je misschien tien keer na
    elkaar. Daarom slaan we het over als er al een verse back-up staat.
    """
    try:
        if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
            return
        recent = backup_lijst()
        if recent and (datetime.now() - recent[0]["moment"]).total_seconds() < minstens_uren * 3600:
            return
        maak_backup("opstart")
    except Exception as fout:                       # nooit de start tegenhouden
        print(f"Let op: de back-up bij het opstarten is niet gelukt ({fout}).")


_backup_bij_opstarten()


def _eigen_adressen(poort):
    """De adressen waarop de site bereikbaar is, om af te drukken bij het starten."""
    import socket
    adressen = [f"http://localhost:{poort}"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # niets wordt echt verstuurd
        adressen.append(f"http://{s.getsockname()[0]}:{poort}")
        s.close()
    except OSError:
        pass
    return adressen


def zet_eigenaar(zoek: str) -> int:
    """Noodluik vanaf de terminal: maak iemand eigenaar (hoofdorganisator).

    Nodig na het bijwerken van een oude database (daar had niemand een rol) of
    als de eigenaar zijn wachtwoord kwijt is. Wie aan de server kan, mag dit —
    die persoon kan sowieso al aan het databasebestand zelf.
    """
    db = verbind(DB_PATH)
    sleutel = zoek.strip().lstrip("#")
    speler = db.execute(
        "SELECT * FROM players WHERE name = ? COLLATE NOCASE OR id = ?",
        (zoek.strip(), int(sleutel) if sleutel.isdigit() else -1)).fetchone()
    if not speler:
        print(f"Geen speler gevonden met naam of nummer “{zoek}”. Bestaande spelers:")
        for r in db.execute("SELECT id, name, role FROM players ORDER BY name"):
            print(f"   #{r['id']}  {r['name']}  ({r['role']})")
        db.close()
        return 1
    db.execute("UPDATE players SET role = ? WHERE role = ?", (ROL_ADMIN, ROL_EIGENAAR))
    db.execute("UPDATE players SET role = ? WHERE id = ?", (ROL_EIGENAAR, speler["id"]))
    db.commit()
    db.close()
    print(f"{speler['name']} (#{speler['id']}) is nu de eigenaar. Laat hem inloggen "
          "met zijn eigen account; het organisatiepaneel staat dan open.")
    return 0


def _waarschuw_zonder_eigenaar():
    """Zeg bij het opstarten hoe je een eigenaar aanduidt, als er nog geen is."""
    db = verbind(DB_PATH)
    aantal = db.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
    heeft = db.execute("SELECT 1 FROM players WHERE role IN (?, ?)",
                       (ROL_ADMIN, ROL_EIGENAAR)).fetchone()
    db.close()
    if aantal and not heeft:
        print("\n  Let op: nog niemand is organisator. Duid een eigenaar aan met:")
        print("      python app.py --eigenaar \"Voornaam Achternaam\"\n")
    elif not aantal:
        print("\n  Nog geen accounts: het eerste account dat zich registreert, "
              "wordt automatisch de eigenaar.\n")


def start(host="0.0.0.0", poort=5000):
    """Start de site met waitress: een echte server die meerdere bezoekers
    tegelijk bedient, in tegenstelling tot de testserver van Flask."""
    _waarschuw_zonder_eigenaar()
    try:
        from waitress import serve
    except ImportError:
        print("waitress ontbreekt (pip install waitress); ik val terug op de "
              "testserver van Flask.")
        app.run(host=host, port=poort, debug=False, threaded=True)
        return

    print("Leberschuss Tonzent draait op:")
    for adres in _eigen_adressen(poort):
        print("   ", adres)
    serve(app, host=host, port=poort, threads=8, ident="Leberschuss Tonzent")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Leberschuss Tonzent")
    p.add_argument("--poort", type=int, default=int(os.environ.get("POORT", 5000)))
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--eigenaar", metavar="NAAM_OF_NUMMER",
                   help="maak deze speler eigenaar (hoofdorganisator) en stop")
    args = p.parse_args()
    if args.eigenaar:
        raise SystemExit(zet_eigenaar(args.eigenaar))
    start(args.host, args.poort)
