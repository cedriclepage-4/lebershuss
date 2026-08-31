# -*- coding: utf-8 -*-
"""Simulatie: bouwt een testdatabase, speelt een volledig toernooi uit en
controleert of stand, shootouts, knockout en ELO's kloppen.

    python test_toernooi.py

Deze test raakt shuss.db NIET aan: hij werkt in een tijdelijke map.
"""

import os
import random
import shutil
import sys
import tempfile

WERKMAP = tempfile.mkdtemp(prefix="shuss_test_")
BRON = os.path.dirname(os.path.abspath(__file__))
for naam in ("app.py", "database.py", "elo.py", "tournament.py"):
    shutil.copy(os.path.join(BRON, naam), WERKMAP)
shutil.copytree(os.path.join(BRON, "templates"), os.path.join(WERKMAP, "templates"))
shutil.copytree(os.path.join(BRON, "static"), os.path.join(WERKMAP, "static"),
                ignore=shutil.ignore_patterns("uploads"))
sys.path.insert(0, WERKMAP)
os.chdir(WERKMAP)

import app as shuss                                              # noqa: E402
import tournament as tm                                          # noqa: E402
from database import verbind                                     # noqa: E402

DB = os.path.join(WERKMAP, "shuss.db")
FOUTEN = []


def check(voorwaarde, tekst):
    print(("  ✔ " if voorwaarde else "  ✘ ") + tekst)
    if not voorwaarde:
        FOUTEN.append(tekst)


def db():
    return verbind(DB)


# ----------------------------------------------------------------- opzet --
print("\n== Testgegevens aanmaken ==")
conn = db()
for i in range(1, 25):
    conn.execute("INSERT INTO players (name, nickname) VALUES (?, ?)",
                 (f"Speler {i:02d}", ""))
for t in range(12):
    conn.execute("INSERT INTO teams (name, player1_id, player2_id, status) "
                 "VALUES (?, ?, ?, 'actief')",
                 (f"Team {chr(65 + t)}", 2 * t + 1, 2 * t + 2))
conn.execute("INSERT INTO seasons (name, start_date, end_date) "
             "VALUES ('Testseizoen', '2026-01-01', '2026-12-31')")
conn.execute("INSERT INTO matchdays (season_id, title, date) "
             "VALUES (1, 'Speeldag 1', '2026-02-01')")
conn.commit()
check(conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 12, "12 teams aangemaakt")

# Een paar ligawedstrijden zodat de permanente ELO al uiteenloopt.
rng = random.Random(7)
for n in range(14):
    a, b = rng.sample(range(1, 13), 2)
    conn.execute("INSERT INTO games (team1_id, team2_id, matchday_id, fase, scheduled_at, "
                 "played_at, status, winner_team_id) VALUES (?, ?, 1, 'liga', ?, ?, "
                 "'gespeeld', ?)", (a, b, f"2026-02-01T{10 + n // 4}:00",
                                    f"2026-02-01T{10 + n // 4}:30", rng.choice([a, b])))
conn.commit()
shuss.herbereken_alles(conn)

elos = [r[0] for r in conn.execute("SELECT elo FROM players")]
check(abs(sum(elos) - 24 * 1000) < 1e-6, "permanente speler-ELO blijft in balans (nulsom)")
seizoen = conn.execute("SELECT COUNT(*) FROM season_ratings WHERE season_id = 1").fetchone()[0]
check(seizoen > 0, f"seizoensratings berekend ({seizoen} rijen)")
sr = {r["entity_id"]: r["elo"] for r in conn.execute(
    "SELECT entity_id, elo FROM season_ratings WHERE entity_type = 'speler'")}
perm = {r["id"]: r["elo"] for r in conn.execute("SELECT id, elo FROM players")}
check(all(abs(sr[p] - perm[p]) < 1e-9 for p in sr),
      "zonder toernooien is de seizoens-ELO gelijk aan de permanente ELO")

# --------------------------------------------------------------- toernooi --
print("\n== Toernooi genereren ==")
conn.execute("""INSERT INTO tournaments (name, date, start_tijd, bracket_ronden,
                ko_teams, potten, slot_minuten)
                VALUES ('Testtoernooi', '2026-03-07', '19:00', 5, 4, 4, 20)""")
for t in range(1, 13):
    conn.execute("INSERT INTO tournament_teams (tournament_id, team_id) VALUES (1, ?)", (t,))
for naam in ("Tafel 1", "Tafel 2", "Tuintafel"):
    conn.execute("INSERT INTO tournament_locations (tournament_id, name) VALUES (1, ?)", (naam,))
conn.commit()

check(tm.controleer(conn, 1) is None, "controle keurt de opzet goed")
ok, boodschap = tm.genereer(conn, 1, rng=random.Random(3))
check(ok, f"generatie: {boodschap}")

games = conn.execute("SELECT * FROM games WHERE tournament_id = 1").fetchall()
check(len(games) == 30, f"30 bracketwedstrijden (12 teams × 5 / 2), gekregen: {len(games)}")

per_team = {}
paren = set()
for g in games:
    for kant in (g["team1_id"], g["team2_id"]):
        per_team[kant] = per_team.get(kant, 0) + 1
    paren.add(tuple(sorted((g["team1_id"], g["team2_id"]))))
check(all(v == 5 for v in per_team.values()), "elk team speelt exact 5 wedstrijden")
check(len(paren) == len(games), "geen enkele affiche komt twee keer voor")

# Tafels: nooit meer wedstrijden tegelijk dan er tafels zijn, en geen team dubbel.
bezet = {}
for g in games:
    bezet.setdefault(g["scheduled_at"], []).append(g)
check(all(len(v) <= 3 for v in bezet.values()), "nooit meer dan 3 wedstrijden tegelijk")
check(len(bezet) == 10, f"30 wedstrijden op 3 tafels = 10 speelrondes, het "
                        f"theoretische minimum (gekregen: {len(bezet)})")
check(all(len({x["location_id"] for x in v}) == len(v) for v in bezet.values()),
      "elke tafel is per tijdslot maar één keer bezet")
check(all(len({x["team1_id"] for x in v} | {x["team2_id"] for x in v}) == 2 * len(v)
          for v in bezet.values()), "geen team speelt twee wedstrijden tegelijk")

potten = {r["team_id"]: r["pot"] for r in conn.execute(
    "SELECT team_id, pot FROM tournament_teams WHERE tournament_id = 1")}
check(sorted(potten.values()) == sorted([1, 2, 3, 4] * 3), "vier potten van drie teams")
sterkste = max(perm, key=lambda p: perm[p])
check(all(v is not None for v in potten.values()), "elk team zit in een pot")

# ------------------------------------------------------------ uitspelen --
print("\n== Bracketfase uitspelen ==")


def speel(game, winnaar=None):
    winnaar = winnaar or rng.choice([game["team1_id"], game["team2_id"]])
    conn.execute("UPDATE games SET status = 'gespeeld', winner_team_id = ?, played_at = ? "
                 "WHERE id = ?", (winnaar, game["scheduled_at"], game["id"]))
    conn.commit()


def _shootouts_beslissen_iets():
    """Elke geplande shootout moet de groep doorstoters kunnen veranderen.

    Anders laat je mensen een wedstrijd spelen die er niet toe doet. "Kunnen"
    telt hier over álle uitslagen van de andere openstaande shootouts heen: een
    wedstrijd die vandaag nog niets beslist maar dat straks wél kan, blijft
    terecht staan. Enkel wie onder géén enkele combinatie iets verandert, hoort
    weg te zijn — en dat is precies wat de opruimstap doet.
    """
    return not tm._wis_zinloze_shootouts(conn, 1)


ronde = 0
zinloos = 0
while True:
    open_games = conn.execute(
        "SELECT * FROM games WHERE tournament_id = 1 AND status = 'gepland' "
        "AND team1_id IS NOT NULL ORDER BY scheduled_at, id").fetchall()
    if not open_games:
        break
    if not _shootouts_beslissen_iets():
        zinloos += 1
    for g in open_games:
        speel(g)
    shuss.herbereken_alles(conn)
    meldingen = tm.evalueer(conn, 1)
    for m in meldingen:
        print("    →", m)
    ronde += 1
    if ronde > 12:
        check(False, "de toernooilus blijft hangen")
        break

t = tm.toernooi(conn, 1)
check(t["status"] == "afgelopen", f"toernooi afgelopen (status: {t['status']})")
check(zinloos == 0, "er wordt nooit een shootout ingepland die niets beslist")

geordend, beslissend = tm.stand(conn, 1)
check(not beslissend, "geen onbesliste plaatsen meer")
check(len(geordend) == 12, "alle 12 teams staan in de stand")
punten = [r["punten"] for r in geordend]
check(punten == sorted(punten, reverse=True), "de stand is aflopend gesorteerd op punten")
check(sum(r["gespeeld"] for r in geordend) == 60, "60 teamdeelnames in de bracketfase")

ko = conn.execute("SELECT * FROM games WHERE tournament_id = 1 AND fase = 'knockout' "
                  "ORDER BY ronde DESC, positie").fetchall()
check(len(ko) == 3, f"knockout: 2 halve finales + finale (gekregen: {len(ko)})")
halve = [g for g in ko if g["ronde"] == 4]
geplaatst = [r["team_id"] for r in geordend[:4]]
check(sorted([halve[0]["team1_id"], halve[0]["team2_id"]]) ==
      sorted([geplaatst[0], geplaatst[3]]), "halve finale 1 = nummer 1 tegen nummer 4")
check(sorted([halve[1]["team1_id"], halve[1]["team2_id"]]) ==
      sorted([geplaatst[1], geplaatst[2]]), "halve finale 2 = nummer 2 tegen nummer 3")
finale = [g for g in ko if g["ronde"] == 2][0]
check(finale["status"] == "gespeeld" and finale["winner_team_id"], "de finale is gespeeld")
check({finale["team1_id"], finale["team2_id"]} ==
      {halve[0]["winner_team_id"], halve[1]["winner_team_id"]},
      "de finalisten zijn de winnaars van de halve finales")

# ------------------------------------------------------------------ elo --
print("\n== ELO-controle ==")
shuss.herbereken_alles(conn)
elos = [r[0] for r in conn.execute("SELECT elo FROM players")]

# Nulsom per wedstrijd: wat de ene wint, verliest de andere — behalve in de
# knockout, waar de verliezer maar de helft betaalt (zie KO_VERLIES in elo.py).
saldi = {}
for r in conn.execute("""
    SELECT g.id, g.fase, SUM(rh.elo_na - rh.elo_voor) AS saldo
    FROM rating_history rh JOIN games g ON g.id = rh.game_id
    WHERE rh.entity_type = 'speler' AND rh.scope = 'permanent'
    GROUP BY g.id
"""):
    saldi.setdefault(r["fase"], []).append(r["saldo"])

for fase in ("liga", "bracket"):
    check(all(abs(s) < 1e-9 for s in saldi.get(fase, [])),
          f"{fase}: wat de winnaar wint, verliest de verliezer (nulsom)")
check(saldi.get("knockout") and all(s > 0 for s in saldi["knockout"]),
      "knockout: de verliezer betaalt maar de helft, dus er komt ELO bij")
check(sum(elos) > 24 * 1000,
      "de totale ELO stijgt daardoor met precies de knockoutbonus")
bonus = sum(saldi.get("knockout", []))
check(abs(sum(elos) - 24 * 1000 - bonus) < 1e-6,
      "en die stijging is exact de som van de knockoutwedstrijden")

sr2 = {r["entity_id"]: r["elo"] for r in conn.execute(
    "SELECT entity_id, elo FROM season_ratings WHERE entity_type = 'speler'")}
check(all(abs(sr2[p] - sr[p]) < 1e-9 for p in sr),
      "toernooiwedstrijden wijzigen de seizoens-ELO NIET")
perm2 = {r["id"]: r["elo"] for r in conn.execute("SELECT id, elo FROM players")}
check(any(abs(perm2[p] - perm[p]) > 1e-6 for p in perm2),
      "toernooiwedstrijden wijzigen de permanente ELO WEL")

kampioen = finale["winner_team_id"]
leden = conn.execute("SELECT player1_id, player2_id FROM teams WHERE id = ?",
                     (kampioen,)).fetchone()
fin_delta = conn.execute("""
    SELECT elo_na - elo_voor AS d FROM rating_history
    WHERE game_id = ? AND entity_type = 'speler' AND entity_id = ? AND scope = 'permanent'
""", (finale["id"], leden[0])).fetchone()["d"]
brackets = [r["d"] for r in conn.execute("""
    SELECT rh.elo_na - rh.elo_voor AS d FROM rating_history rh
    JOIN games g ON g.id = rh.game_id
    WHERE g.fase = 'bracket' AND rh.entity_id = ? AND rh.entity_type = 'speler'
      AND rh.scope = 'permanent' AND rh.elo_na > rh.elo_voor
""", (leden[0],))]
check(fin_delta > 0, f"de finalewinst levert ELO op (+{fin_delta:.1f})")
if brackets:
    check(fin_delta > max(brackets),
          f"finale (+{fin_delta:.1f}) weegt zwaarder dan elke bracketwinst "
          f"(max +{max(brackets):.1f})")

# -------------------------------------------------------- oneven teams --
# ------------------------------------------- uitslagen rechtzetten (wissen) --
print("\n== Uitslagen wissen en opnieuw spelen ==")
check(tm.toernooi(conn, 1)["status"] == "afgelopen", "het toernooi staat op afgelopen")

# Een bracketuitslag mag niet weg zolang de knockout erop voortbouwt.
eerste_bracket = conn.execute("SELECT * FROM games WHERE tournament_id = 1 "
                              "AND fase = 'bracket' LIMIT 1").fetchone()
check(tm.mag_wissen(conn, eerste_bracket) is not None,
      "een bracketuitslag wissen wordt geweigerd zolang de knockout al gespeeld is")

# De finale wissen mag wél — en dan mag het toernooi niet meer 'afgelopen' zijn.
finale_rij = conn.execute("SELECT * FROM games WHERE tournament_id = 1 "
                          "AND fase = 'knockout' AND ronde = 2").fetchone()
check(tm.mag_wissen(conn, finale_rij) is None, "de finale mag gewist worden")
conn.execute("UPDATE games SET status = 'gepland', winner_team_id = NULL, played_at = NULL "
             "WHERE id = ?", (finale_rij["id"],))
conn.commit()
tm.herstel_na_wissen(conn, 1, finale_rij["fase"], finale_rij)
shuss.herbereken_alles(conn)
tm.evalueer_alles(conn)
check(tm.toernooi(conn, 1)["status"] == "knockout",
      "na het wissen van de finale springt het toernooi terug naar 'knockout'")
opnieuw = conn.execute("SELECT * FROM games WHERE id = ?", (finale_rij["id"],)).fetchone()
check(bool(opnieuw["team1_id"]) and bool(opnieuw["team2_id"]),
      "de finalisten blijven staan als enkel de finale gewist wordt")

# Een halve finale wissen moet de finalist die eruit voortkwam weer weghalen.
halve_rij = conn.execute("SELECT * FROM games WHERE tournament_id = 1 AND fase = 'knockout' "
                         "AND ronde = 4 AND status = 'gespeeld' LIMIT 1").fetchone()
conn.execute("UPDATE games SET status = 'gepland', winner_team_id = NULL, played_at = NULL "
             "WHERE id = ?", (halve_rij["id"],))
conn.commit()
tm.herstel_na_wissen(conn, 1, halve_rij["fase"], halve_rij)
kolom = "team1_id" if halve_rij["volgende_slot"] == 1 else "team2_id"
volgende = conn.execute("SELECT * FROM games WHERE id = ?",
                        (halve_rij["volgende_game_id"],)).fetchone()
check(volgende[kolom] is None,
      "de finale verliest de winnaar van een gewiste halve finale")

# En daarna moet alles gewoon opnieuw uitgespeeld kunnen worden.
for _ in range(10):
    nog_open = conn.execute("SELECT * FROM games WHERE tournament_id = 1 "
                            "AND status = 'gepland' AND team1_id IS NOT NULL").fetchall()
    if not nog_open:
        break
    for g in nog_open:
        speel(g)
    shuss.herbereken_alles(conn)
    tm.evalueer(conn, 1)
check(tm.toernooi(conn, 1)["status"] == "afgelopen",
      "na het rechtzetten kan het toernooi gewoon opnieuw uitgespeeld worden")



# ------------------------------------------------- tiebreak zonder ELO-verschil --
print("\n== Tiebreak wanneer iedereen op 1000 begint ==")
# Zelfde ELO voor iedereen: de volgorde mag niet op de naam berusten.
conn.execute("UPDATE teams SET elo = 1000")
conn.commit()
namen_orde = [r["naam"] for r in tm.stand(conn, 1)[0]]
check(namen_orde != sorted(namen_orde),
      "de stand is niet zomaar alfabetisch wanneer alle ELO's gelijk zijn")

# De kwaliteit van de overwinningen (punten van wie je klopte) moet de teams
# scheiden, en mag NIET afhangen van het moment waarop je iemand trof.
geordend_nu, _ = tm.stand(conn, 1)
punten_van = {r["team_id"]: r["punten"] for r in geordend_nu}
kwaliteit, programma = tm.winstkwaliteit(conn, 1, punten_van)
check(len(set(kwaliteit.values())) > 1,
      "de kwaliteit van de overwinningen verschilt tussen de teams")
for r in geordend_nu:
    verwacht_kw = sum(punten_van[g["team2_id"] if g["winner_team_id"] == g["team1_id"]
                                 else g["team1_id"]]
                      for g in conn.execute(
                          "SELECT * FROM games WHERE tournament_id = 1 AND fase = 'bracket' "
                          "AND status = 'gespeeld' AND winner_team_id = ?", (r["team_id"],)))
    if r["kwaliteit"] != verwacht_kw:
        check(False, f"kwaliteit klopt niet voor {r['naam']}")
        break
else:
    check(True, "kwaliteit = som van de punten van de verslagen teams")

# De toernooikracht (gewonnen/verloren ELO tijdens de bracketfase) moet
# verschillen, ook al start iedereen op dezelfde rating.
kracht = tm.elo_na_bracketfase(conn, 1)
check(len(set(round(v, 6) for v in kracht.values())) > 1,
      "de toernooikracht scheidt teams die op punten gelijk staan")
check(abs(sum(kracht.values())) < 1e-6,
      "de toernooikracht is een nulsom: iedereen begint vanavond op 0")

# Knockoutwedstrijden mogen de bracketstand niet meer verschuiven.
voor = [r["team_id"] for r in tm.stand(conn, 1)[0]]
shuss.herbereken_alles(conn)
tm.evalueer_alles(conn)
na = [r["team_id"] for r in tm.stand(conn, 1)[0]]
check(voor == na, "de bracketstand blijft identiek na een herberekening")

# De potindeling mag niet alfabetisch zijn bij gelijke ELO.
verdelingen = set()
for zaad in range(4):
    teams_lijst = [dict(id=i, naam=f"Team {chr(65+i)}", elo=1000.0) for i in range(12)]
    verdelingen.add(tuple(sorted(tm.potten_verdelen(
        teams_lijst, 4, random.Random(zaad)).items())))
check(len(verdelingen) > 1,
      "de potindeling verschilt per loting wanneer alle teams even sterk zijn")


# ---------------------------------------------- shootouts die in een kring draaien --
print("\n== Kringetje bij shootouts ==")


def _nep(gid, a, b, w):
    return {"id": gid, "team1_id": a, "team2_id": b, "winner_team_id": w,
            "status": "gespeeld", "fase": "shootout",
            "scheduled_at": f"2026-09-04T2{gid}:00"}


drie = [{"team_id": i, "naam": f"T{i}", "elo": 1000.0, "kracht": 1.0 - i} for i in (1, 2, 3)]

# Nog niets gespeeld: er moet wél een shootoutronde komen.
nodig = []
tm._orden_groep([dict(r) for r in drie], [], [], nodig, 1)
check(len(nodig) == 1, "drie gelijke teams krijgen een shootoutronde")

# A klopt B, B klopt C, C klopt A: iedereen 1 winst, 1 verlies.
kringetje = [_nep(1, 1, 2, 1), _nep(2, 2, 3, 2), _nep(3, 3, 1, 3)]
nodig = []
volgorde = tm._orden_groep([dict(r) for r in drie], [], kringetje, nodig, 1)
check(not nodig, "na een kringetje wordt er GEEN nieuwe shootoutronde gepland")
check([r["team_id"] for r in volgorde] == [1, 2, 3],
      "een kringetje wordt op toernooikracht beslist")

# Eén gespeelde shootout is genoeg: ook als de teams daarna nog gelijk staan,
# komt er geen tweede ronde. Zo weet de zaal bij de start hoeveel er komen.
half = [_nep(1, 1, 2, 1), _nep(2, 3, 1, 3)]        # 2 en 3 speelden niet onderling
nodig = []
tm._orden_groep([dict(r) for r in drie], [], half, nodig, 1)
check(not nodig, "wie zijn shootout speelde, krijgt er geen tweede")

# Een gewone uitslag (geen kring) blijft gewoon werken.
beslist = [_nep(1, 1, 2, 1), _nep(2, 1, 3, 1), _nep(3, 2, 3, 2)]
nodig = []
tm._orden_groep([dict(r) for r in drie], [], beslist, nodig, 1)
check(not nodig, "een shootoutronde met een duidelijke winnaar is meteen klaar")

# Groep van vier: één ronde, en daarna beslist de toernooikracht. Vroeger
# speelden de winnaars nog onderling verder; dat gaf onaangekondigde extra
# rondes en is nu bewust afgeschaft.
vier = [{"team_id": i, "naam": f"T{i}", "elo": 1000.0, "kracht": -i} for i in (1, 2, 3, 4)]
nodig = []
tm._orden_groep([dict(r) for r in vier], [], [], nodig, 1)
check(len(nodig) == 1, "vier gelijke teams krijgen één shootoutronde")
nodig = []
volgorde = tm._orden_groep([dict(r) for r in vier], [],
                           [_nep(1, 1, 2, 1), _nep(2, 3, 4, 3)], nodig, 1)
check(not nodig, "na die ene ronde komt er geen tweede, ook niet voor de winnaars")
check([r["team_id"] for r in volgorde][:2] == [1, 3],
      "de winnaars staan bovenaan, onderling geordend op toernooikracht")

print("\n== Oneven aantal teams ==")
for naam in ("Speler 25", "Speler 26"):
    conn.execute("INSERT INTO players (name) VALUES (?)", (naam,))
conn.execute("INSERT INTO teams (name, player1_id, player2_id, status) "
             "VALUES ('Team M', 25, 26, 'actief')")
conn.execute("""INSERT INTO tournaments (name, date, start_tijd, bracket_ronden,
                ko_teams, potten, slot_minuten)
                VALUES ('Onevencup', '2026-04-04', '19:00', 3, 4, 4, 20)""")
for t in range(1, 14):
    conn.execute("INSERT INTO tournament_teams (tournament_id, team_id) VALUES (2, ?)", (t,))
for naam in ("Tafel 1", "Tafel 2"):
    conn.execute("INSERT INTO tournament_locations (tournament_id, name) VALUES (2, ?)",
                 (naam,))
conn.commit()

melding = tm.controleer(conn, 2)
check(melding is not None and "even aantal wedstrijden" in melding,
      "13 teams × 3 wedstrijden wordt geweigerd met uitleg")
conn.execute("UPDATE tournaments SET bracket_ronden = 4 WHERE id = 2")
conn.commit()
check(tm.controleer(conn, 2) is None, "13 teams × 4 wedstrijden wordt aanvaard")

ok, boodschap = tm.genereer(conn, 2, rng=random.Random(9))
check(ok, f"generatie met 13 teams: {boodschap}")
games2 = conn.execute("SELECT * FROM games WHERE tournament_id = 2").fetchall()
per_team2 = defaultdict(int) if False else {}
paren2 = set()
for g in games2:
    for kant in (g["team1_id"], g["team2_id"]):
        per_team2[kant] = per_team2.get(kant, 0) + 1
    paren2.add(tuple(sorted((g["team1_id"], g["team2_id"]))))
check(len(games2) == 26, f"26 wedstrijden (13 × 4 / 2), gekregen: {len(games2)}")
check(sorted(set(per_team2.values())) == [4], "élk team speelt exact 4 wedstrijden")
check(len(per_team2) == 13, "alle 13 teams komen aan de beurt")
check(len(paren2) == len(games2), "geen dubbele affiches")

bezet2 = {}
for g in games2:
    bezet2.setdefault(g["scheduled_at"], []).append(g)
check(all(len({x["team1_id"] for x in v} | {x["team2_id"] for x in v}) == 2 * len(v)
          for v in bezet2.values()), "geen team speelt twee wedstrijden tegelijk")
check(all(len(v) <= 2 for v in bezet2.values()), "nooit meer wedstrijden dan tafels")
rondes2 = {g["ronde"] for g in games2}
check(len(rondes2) == 13, f"26 wedstrijden op 2 tafels = 13 speelrondes, het "
                          f"theoretische minimum (gekregen: {len(rondes2)})")
check(all(len(v) == 2 for v in bezet2.values()),
      "beide tafels zijn in élke speelronde bezet (geen leegloop)")

# ------------------------------------------------------------- pagina's --
print("\n== Pagina's opvragen ==")
shuss.app.config["TESTING"] = True
klant = shuss.app.test_client()
# Organisator zijn = ingelogd zijn met een account dat die rol heeft.
conn.execute("UPDATE players SET role ='eigenaar' WHERE id = 1")
conn.commit()
with klant.session_transaction() as s:
    s["speler_id"] = 1
for pad in ["/", "/statistieken", "/seizoenen", "/seizoen/1",
            "/speler/1", "/team/1", "/toernooien", "/toernooi/1", "/toernooi/1/loting",
            "/admin", "/admin/spelers", "/admin/toernooi/1"]:
    antwoord = klant.get(pad)
    check(antwoord.status_code == 200, f"{pad} → {antwoord.status_code}")

# Een gewone speler mag niet in het organisatiepaneel.
conn.execute("UPDATE players SET role ='speler' WHERE id = 1")
conn.commit()
check(klant.get("/admin").status_code == 302, "gewone speler wordt weggestuurd bij /admin")
check(klant.get("/admin/spelers").status_code == 302,
      "gewone speler wordt weggestuurd bij /admin/spelers")

# ------------------------------------------------- league aan en uit --
print("\n== Leaguegedeelte aan- en uitzetten ==")
# Enkel deze horen bij de league; Home (/) en de statistieken gaan over álle
# wedstrijden en blijven dus altijd bereikbaar.
LEAGUE_PADEN = ["/seizoenen", "/seizoen/1"]
ALTIJD_OPEN = ["/", "/statistieken", "/toernooien"]


def zet_league(aan):
    conn.execute("UPDATE settings SET value = ? WHERE key = 'league_actief'",
                 ("1" if aan else "0",))
    conn.commit()


zet_league(False)      # speler_id 1 is hier een gewone speler
check(all(klant.get(p).status_code == 302 for p in LEAGUE_PADEN),
      "met de league uit komt een speler op geen enkele leaguepagina")
check(klant.get("/seizoenen").headers["Location"].endswith("/"),
      "hij belandt dan op de thuispagina")
check(all(klant.get(p).status_code == 200 for p in ALTIJD_OPEN),
      "Home, statistieken en toernooi blijven gewoon open")
thuis = klant.get("/").get_data(as_text=True)
check("Globaal klassement" in thuis, "de thuispagina toont het globale klassement")
check(">🏆</span> League" not in thuis, "de Leagueknop verdwijnt uit de balk")
check(">🏠</span> Home" in thuis and ">🥇</span> Toernooi" in thuis,
      "Home en Toernooi blijven wel staan")

conn.execute("UPDATE players SET role = 'eigenaar' WHERE id = 1")
conn.commit()
check(all(klant.get(p).status_code == 200 for p in LEAGUE_PADEN),
      "een organisator ziet de leaguepagina's ook als ze uit staan")

zet_league(True)
conn.execute("UPDATE players SET role = 'speler' WHERE id = 1")
conn.commit()
check(all(klant.get(p).status_code == 200 for p in LEAGUE_PADEN),
      "met de league aan ziet iedereen ze weer")
thuis = klant.get("/").get_data(as_text=True)
check(">🏆</span> League" in thuis, "en staat de Leagueknop weer in de balk")
check(thuis.count("sectie-knop") == 3, "er zijn dan drie tabbladen")

# --------------------------------------------- accounts opeisen (claim) --
print("\n== Accounts opeisen ==")
conn.execute("INSERT INTO players (id, name, elo) VALUES (4242, 'Nieuwe Speler', 1000)")
conn.commit()
gast = shuss.app.test_client()


def zet_claim(open_):
    conn.execute("UPDATE settings SET value = ? WHERE key = 'claim_open'",
                 ("1" if open_ else "0",))
    conn.commit()


def claim(client, **data):
    return client.post("/claimen/4242", data=data, follow_redirects=True).get_data(as_text=True)


zet_claim(False)
claim(gast, wachtwoord="zomaar123", herhaal="zomaar123")
check(conn.execute("SELECT password_hash FROM players WHERE id = 4242").fetchone()[0] is None,
      "met het venster dicht kan een account niet opgeëist worden")
check(gast.get("/claimen", follow_redirects=True).request.path == "/inloggen",
      "en is de keuzelijst zelf ook niet te openen")

zet_claim(True)
check("/claimen/4242" in gast.get("/claimen").get_data(as_text=True),
      "stap 1 toont de naam als aanklikbare keuze")
check('type="password"' not in gast.get("/claimen").get_data(as_text=True),
      "stap 1 vraagt nog niets in te vullen")
check('name="bijnaam"' in gast.get("/claimen/4242").get_data(as_text=True),
      "stap 2 vraagt bijnaam en wachtwoord voor die ene naam")

claim(gast, wachtwoord="kort", herhaal="kort")
check(conn.execute("SELECT password_hash FROM players WHERE id = 4242").fetchone()[0] is None,
      "een te kort wachtwoord wordt geweigerd")
claim(gast, wachtwoord="zomaar123", herhaal="anders123")
check(conn.execute("SELECT password_hash FROM players WHERE id = 4242").fetchone()[0] is None,
      "twee verschillende wachtwoorden worden geweigerd")

claim(gast, bijnaam="Nieuweling", wachtwoord="zomaar123", herhaal="zomaar123")
rij = conn.execute("SELECT name, nickname, password_hash FROM players WHERE id = 4242").fetchone()
check(rij["password_hash"] is not None, "met het venster open lukt het opeisen wel")
check(rij["name"] == "Nieuwe Speler", "de echte naam blijft onaangeroerd bij het opeisen")
check(rij["nickname"] == "Nieuweling", "de gekozen bijnaam is bewaard")
check(conn.execute("SELECT COUNT(*) FROM claim_log WHERE player_id = 4242 "
                   "AND soort = 'claim'").fetchone()[0] == 1, "de claim staat in het logboek")

tweede = shuss.app.test_client()
claim(tweede, wachtwoord="kaper1234", herhaal="kaper1234")
check(conn.execute("SELECT password_hash FROM players WHERE id = 4242").fetchone()[0]
      == rij["password_hash"], "een tweede claim van hetzelfde account verandert niets")
check(tweede.get("/claimen/4242", follow_redirects=True).request.path == "/claimen",
      "wie te laat is, komt terug bij de keuzelijst")
check("Nieuwe Speler" not in gast.get("/claimen").get_data(as_text=True),
      "een opgeëist account staat niet meer in de lijst")
zet_claim(False)

print("\n" + "=" * 60)
if FOUTEN:
    print(f"❌ {len(FOUTEN)} test(s) gefaald:")
    for f in FOUTEN:
        print("   -", f)
    sys.exit(1)
print("✅ Alle tests geslaagd.")
print("=" * 60)
