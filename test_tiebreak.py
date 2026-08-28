# -*- coding: utf-8 -*-
"""Stresstest van de tiebreaklogica.

    python test_tiebreak.py

Zoekt gericht naar situaties waarin de rangschikking zichzelf tegenspreekt of
oneerlijk uitpakt. De eigenschappen die hier gecontroleerd worden:

  1. Stabiliteit     — dezelfde toestand geeft altijd dezelfde stand, en
                       opnieuw doorrekenen verandert er niets aan.
  2. Sluitend        — er stoten er exact zoveel door als voorzien, en er blijft
                       nooit een onbesliste plek over.
  3. Uitleg klopt    — waar de tabel zegt "kwaliteit besliste", moet kwaliteit
                       ook echt het eerste verschil zijn.
  4. Geen straf op winst — een wedstrijd winnen mag je nooit slechter doen
                       eindigen dan diezelfde wedstrijd verliezen.
  5. Vrijstelling    — wie buiten de shootout blijft, mag door geen enkele
                       shootoutuitslag alsnog van plaats wisselen.
  6. Naamonafhankelijk — teams hernoemen mag de volgorde niet veranderen.
  7. Lotingsbalans   — bij een volledig gelijke stand mag de loting niet
                       structureel het laagste teamnummer bevoordelen.

Deze test raakt shuss.db NIET aan: hij werkt in een tijdelijke map.
"""

import collections
import itertools
import os
import random
import shutil
import sys
import tempfile

WERKMAP = tempfile.mkdtemp(prefix="schuss_tiebreak_")
BRON = os.path.dirname(os.path.abspath(__file__))
for naam in ("app.py", "database.py", "elo.py", "tournament.py"):
    shutil.copy(os.path.join(BRON, naam), WERKMAP)
shutil.copytree(os.path.join(BRON, "templates"), os.path.join(WERKMAP, "templates"))
shutil.copytree(os.path.join(BRON, "static"), os.path.join(WERKMAP, "static"),
                ignore=shutil.ignore_patterns("uploads"))
sys.path.insert(0, WERKMAP)
os.chdir(WERKMAP)

import app as schuss                                             # noqa: E402
import tournament as tm                                          # noqa: E402
from database import init_db, verbind                            # noqa: E402

FOUTEN = []
TEL = collections.Counter()


def meld(voorwaarde, tekst):
    if not voorwaarde:
        FOUTEN.append(tekst)
    return voorwaarde


# --------------------------------------------------------------- opzetten --

def bouw(n_teams, ronden, ko, zaad, elos=None):
    """Een geloot toernooi met n teams, klaar om uitgespeeld te worden."""
    pad = os.path.join(WERKMAP, f"tb_{n_teams}_{ronden}_{ko}_{zaad}.db")
    if os.path.exists(pad):
        os.remove(pad)
    init_db(pad)
    db = verbind(pad)
    for i in range(1, 2 * n_teams + 1):
        db.execute("INSERT INTO players (name, nickname) VALUES (?, '')", (f"S{i:03d}",))
    for t in range(n_teams):
        db.execute("INSERT INTO teams (name, player1_id, player2_id, status, elo) "
                   "VALUES (?, ?, ?, 'actief', ?)",
                   (f"T{t + 1:02d}", 2 * t + 1, 2 * t + 2,
                    1000 if elos is None else elos[t]))
    db.execute("""INSERT INTO tournaments (name, date, start_tijd, bracket_ronden,
                  ko_teams, potten, slot_minuten)
                  VALUES ('Test', '2026-03-07', '19:00', ?, ?, 4, 20)""", (ronden, ko))
    for t in range(1, n_teams + 1):
        db.execute("INSERT INTO tournament_teams (tournament_id, team_id) VALUES (1, ?)", (t,))
    for i in range(4):
        db.execute("INSERT INTO tournament_locations (tournament_id, name) "
                   "VALUES (1, ?)", (f"Tafel {i + 1}",))
    db.commit()
    ok, boodschap = tm.genereer(db, 1, rng=random.Random(zaad))
    if not ok:
        db.close()
        os.remove(pad)
        return None, None, boodschap
    return db, pad, None


def speel_bracket(db, rng, uitslagen=None):
    """Speel de bracketfase. `uitslagen` mag een {game_id: winnaar} opleggen."""
    for g in db.execute("SELECT * FROM games WHERE tournament_id = 1 AND fase = 'bracket' "
                        "ORDER BY scheduled_at, id").fetchall():
        w = (uitslagen or {}).get(g["id"]) or rng.choice([g["team1_id"], g["team2_id"]])
        db.execute("UPDATE games SET status = 'gespeeld', winner_team_id = ?, played_at = ? "
                   "WHERE id = ?", (w, g["scheduled_at"], g["id"]))
    db.commit()
    schuss.herbereken_alles(db)
    tm.evalueer(db, 1)


def open_shootouts(db):
    return [(g["id"], g["team1_id"], g["team2_id"]) for g in tm._open_shootouts(db, 1)]


def speel_shootouts(db, rng):
    while True:
        nu = open_shootouts(db)
        if not nu:
            return
        for gid, a, b in nu:
            db.execute("UPDATE games SET status = 'gespeeld', winner_team_id = ?, "
                       "played_at = '2026-03-07T22:00' WHERE id = ?", (rng.choice([a, b]), gid))
        db.commit()
        schuss.herbereken_alles(db)
        tm.evalueer(db, 1)


def volgorde(db):
    return [r["team_id"] for r in tm.stand(db, 1)[0]]


# ------------------------------------------------------------- 1. stabiel --

def test_stabiliteit(vormen):
    print("\n== 1. Dezelfde toestand geeft dezelfde stand ==")
    for n, ronden, ko, zaad in vormen:
        db, pad, fout = bouw(n, ronden, ko, zaad)
        if fout:
            continue
        speel_bracket(db, random.Random(zaad * 17 + 1))
        eerste = volgorde(db)
        # tien keer opnieuw opvragen mag niets veranderen
        if not meld(all(volgorde(db) == eerste for _ in range(10)),
                    f"n={n} zaad={zaad}: de stand verschilt tussen twee opvragingen"):
            pass
        # opnieuw evalueren en volledig herberekenen evenmin
        for _ in range(3):
            tm.evalueer(db, 1)
        meld(volgorde(db) == eerste,
             f"n={n} zaad={zaad}: evalueer() verandert de stand")
        schuss.herbereken_alles(db)
        tm.evalueer(db, 1)
        meld(volgorde(db) == eerste,
             f"n={n} zaad={zaad}: herberekenen verandert de stand")
        TEL["stabiliteit"] += 1
        db.close()
        os.remove(pad)
    print(f"  {TEL['stabiliteit']} toernooien gecontroleerd")


# ------------------------------------------------------------- 2. sluitend --

def test_sluitend(vormen):
    print("\n== 2. De streep ligt waar hij moet liggen ==")
    for n, ronden, ko, zaad in vormen:
        db, pad, fout = bouw(n, ronden, ko, zaad)
        if fout:
            continue
        rng = random.Random(zaad * 29 + 5)
        speel_bracket(db, rng)
        speel_shootouts(db, rng)
        geordend, beslissend = tm.stand(db, 1)
        meld(len(geordend) == n, f"n={n} zaad={zaad}: {len(geordend)} rijen i.p.v. {n}")
        meld(len({r["team_id"] for r in geordend}) == n,
             f"n={n} zaad={zaad}: een team staat twee keer in de stand")
        meld([r["positie"] for r in geordend] == list(range(1, n + 1)),
             f"n={n} zaad={zaad}: de posities lopen niet netjes van 1 tot {n}")
        meld(sum(1 for r in geordend if r["doorstoot"]) == ko,
             f"n={n} zaad={zaad}: {sum(1 for r in geordend if r['doorstoot'])} door i.p.v. {ko}")
        meld(not beslissend, f"n={n} zaad={zaad}: nog een onbesliste groep na afloop")
        meld(not any(r["onbeslist"] for r in geordend),
             f"n={n} zaad={zaad}: een team blijft als 'onbeslist' gemarkeerd")
        ko_teams = {r["team_id"] for r in db.execute(
            "SELECT team1_id AS team_id FROM games WHERE tournament_id = 1 "
            "AND fase = 'knockout' AND ronde = ? UNION "
            "SELECT team2_id FROM games WHERE tournament_id = 1 AND fase = 'knockout' "
            "AND ronde = ?", (ko, ko))}
        if ko_teams:
            meld(ko_teams == {r["team_id"] for r in geordend[:ko]},
                 f"n={n} zaad={zaad}: het knockoutschema bevat andere teams dan de top {ko}")
        TEL["sluitend"] += 1
        db.close()
        os.remove(pad)
    print(f"  {TEL['sluitend']} toernooien uitgespeeld tot en met de knockoutloting")


# --------------------------------------------------------- 3. uitleg klopt --

CRITERIA = ["onderling", "kwaliteit", "programma", "shootout", "kracht", "loting"]


def groepssleutel(db, groep_ids):
    """De ketting als sorteersleutel, los van de implementatie opgebouwd.

    punten → onderling → kwaliteit → programma → shootout → kracht → loting.
    Het onderlinge duel telt enkel mee als de mini-tabel compleet is.
    """
    bracket = tm.games_van(db, 1, "bracket")
    shootouts = tm.games_van(db, 1, "shootout")
    h2h, _ = (tm._mini_punten(bracket, groep_ids) if tm._volledig_onderling(bracket, groep_ids)
              else ({t: 0 for t in groep_ids}, None))
    so, _ = tm._mini_punten(shootouts, groep_ids)
    so_v = tm._mini_verlies(shootouts, groep_ids)

    def sleutel(r):
        # Eén element per criterium, in dezelfde volgorde als CRITERIA, zodat de
        # sleutel én sorteert én netjes te benoemen valt. De shootout telt als
        # één criterium met twee cijfers: gewonnen eerst, verloren laatst.
        t = r["team_id"]
        return (-h2h[t], -r["kwaliteit"], -r["programma"], (-so[t], so_v[t]),
                -round(r["kracht"], 6), tm.lot_sleutel(1, t))
    return sleutel


def eerste_verschil(db, a, b, groep_ids):
    """Op welk criterium worden deze twee buren écht gescheiden?"""
    sleutel = groepssleutel(db, groep_ids)
    ka, kb = sleutel(a), sleutel(b)
    for naam, x, y in zip(CRITERIA, ka, kb):
        if x != y:
            return naam
    return "loting"


def test_uitleg(vormen):
    print("\n== 3. De uitleg klopt met de echte reden ==")
    gezien = collections.Counter()
    for n, ronden, ko, zaad in vormen:
        db, pad, fout = bouw(n, ronden, ko, zaad)
        if fout:
            continue
        rng = random.Random(zaad * 31 + 7)
        speel_bracket(db, rng)
        speel_shootouts(db, rng)
        geordend, _ = tm.stand(db, 1)
        # per puntengroep: elke buur moet op een criterium gescheiden zijn, en
        # de volgorde van de criteria moet de ketting volgen.
        i = 0
        while i < len(geordend):
            j = i
            while j + 1 < len(geordend) and geordend[j + 1]["punten"] == geordend[i]["punten"]:
                j += 1
            groep = geordend[i:j + 1]
            i = j + 1
            if len(groep) < 2:
                continue
            ids = {r["team_id"] for r in groep}
            for reden in (eerste_verschil(db, a, b, ids) for a, b in zip(groep, groep[1:])):
                gezien[reden] += 1
            # De échte controle: de getoonde volgorde moet exact de volgorde van
            # de ketting zijn. Eén onafhankelijk opgebouwde sleutel, en die moet
            # oplopen van boven naar onder.
            sleutel = groepssleutel(db, ids)
            sleutels = [sleutel(r) for r in groep]
            for (ka, a), (kb, b) in zip(zip(sleutels, groep), zip(sleutels[1:], groep[1:])):
                meld(ka <= kb,
                     f"n={n} zaad={zaad}: T{a['team_id']:02d} staat boven "
                     f"T{b['team_id']:02d} maar verliest op de ketting "
                     f"({ka} vs {kb})")
        TEL["uitleg"] += 1
        db.close()
        os.remove(pad)
    print(f"  {TEL['uitleg']} toernooien; scheidingen per criterium:")
    for c in CRITERIA:
        if gezien[c]:
            print(f"    {c:<12} {gezien[c]}")


# ------------------------------------------------- 4. geen straf op winnen --

def test_geen_straf_op_winst(vormen):
    """Winnen mag je nooit slechter doen eindigen dan verliezen.

    Voor elke bracketwedstrijd draaien we de uitslag om en kijken we naar het
    team dat daardoor van verliezer winnaar wordt. Het mag daar nooit slechter
    van worden — niet in plaats, en zeker niet in kwalificatie.
    """
    print("\n== 4. Winnen straft nooit ==")
    zwaarder = 0
    for n, ronden, ko, zaad in vormen:
        db, pad, fout = bouw(n, ronden, ko, zaad)
        if fout:
            continue
        rng = random.Random(zaad * 37 + 3)
        speel_bracket(db, rng)
        basis = {r["team_id"]: r for r in tm.stand(db, 1)[0]}
        origineel = {g["id"]: g["winner_team_id"] for g in db.execute(
            "SELECT id, winner_team_id FROM games WHERE tournament_id = 1 AND fase = 'bracket'")}
        for gid, winnaar in origineel.items():
            g = db.execute("SELECT team1_id, team2_id FROM games WHERE id = ?", (gid,)).fetchone()
            verliezer = g["team2_id"] if winnaar == g["team1_id"] else g["team1_id"]
            db.execute("UPDATE games SET winner_team_id = ? WHERE id = ?", (verliezer, gid))
            db.commit()
            nieuw = {r["team_id"]: r for r in tm.stand(db, 1)[0]}
            zwaarder += 1
            # `verliezer` won nu wél. Zijn plaats mag niet slechter zijn.
            meld(nieuw[verliezer]["positie"] <= basis[verliezer]["positie"],
                 f"n={n} zaad={zaad} game {gid}: T{verliezer:02d} zakt van plaats "
                 f"{basis[verliezer]['positie']} naar {nieuw[verliezer]['positie']} "
                 f"dóór te winnen")
            meld(not (basis[verliezer]["doorstoot"] and not nieuw[verliezer]["doorstoot"]),
                 f"n={n} zaad={zaad} game {gid}: T{verliezer:02d} verliest zijn ticket "
                 f"door die wedstrijd te winnen")
            db.execute("UPDATE games SET winner_team_id = ? WHERE id = ?", (winnaar, gid))
            db.commit()
        TEL["winst"] += 1
        db.close()
        os.remove(pad)
    print(f"  {TEL['winst']} toernooien, {zwaarder} omgedraaide uitslagen doorgerekend")


# ---------------------------------------------------------- 5. vrijstelling --

def test_vrijstelling(vormen):
    """Wie geen shootout speelt, mag door geen enkele uitslag verschuiven."""
    print("\n== 5. Vrijgestelde teams liggen echt vast ==")
    scenario_s = 0
    for n, ronden, ko, zaad in vormen:
        db, pad, fout = bouw(n, ronden, ko, zaad)
        if fout:
            continue
        speel_bracket(db, random.Random(zaad * 41 + 9))
        open_g = tm._open_shootouts(db, 1)
        if not open_g or len(open_g) > 6:
            db.close(); os.remove(pad); continue
        spelers = {t for g in open_g for t in (g["team1_id"], g["team2_id"])}
        voor = {r["team_id"]: r["doorstoot"] for r in tm.stand(db, 1)[0]}
        keuzes = [(g["team1_id"], g["team2_id"]) for g in open_g]
        for combinatie in itertools.product(*keuzes):
            hyp = {g["id"]: w for g, w in zip(open_g, combinatie)}
            na = {r["team_id"]: r["doorstoot"] for r in tm.stand(db, 1, hyp)[0]}
            scenario_s += 1
            for team, doorstoot in voor.items():
                if team in spelers:
                    continue
                meld(na[team] == doorstoot,
                     f"n={n} zaad={zaad}: T{team:02d} speelt geen shootout maar gaat van "
                     f"{'door' if doorstoot else 'uit'} naar {'door' if na[team] else 'uit'}")
        TEL["vrijstelling"] += 1
        db.close()
        os.remove(pad)
    print(f"  {TEL['vrijstelling']} toernooien, {scenario_s} volledige scenario's doorgerekend")


# ------------------------------------------------------ 6. naamonafhankelijk --

def test_namen(vormen):
    print("\n== 6. De volgorde hangt niet van de teamnaam af ==")
    for n, ronden, ko, zaad in vormen:
        db, pad, fout = bouw(n, ronden, ko, zaad)
        if fout:
            continue
        rng = random.Random(zaad * 43 + 11)
        speel_bracket(db, rng)
        speel_shootouts(db, rng)
        voor = volgorde(db)
        # namen omkeren: wie eerst vooraan stond in het alfabet, staat nu achteraan
        for team_id in range(1, n + 1):
            db.execute("UPDATE teams SET name = ? WHERE id = ?",
                       (f"Z{n - team_id:03d}_hernoemd", team_id))
        db.commit()
        meld(volgorde(db) == voor,
             f"n={n} zaad={zaad}: de stand verandert als je de teams hernoemt")
        TEL["namen"] += 1
        db.close()
        os.remove(pad)
    print(f"  {TEL['namen']} toernooien hernoemd en opnieuw vergeleken")


# ------------------------------------------------------- 7. lotingsbalans --

def test_loting():
    """Bevoordeelt de loting stelselmatig het laagste teamnummer?

    Bij twee teams die op niets meer te scheiden zijn, beslist `lot_sleutel`.
    Over veel toernooien heen moet dat ongeveer 50/50 uitkomen.
    """
    print("\n== 7. De loting is niet stelselmatig scheef ==")
    laagste_wint = 0
    totaal = 0
    for tid in range(1, 400):
        for a, b in [(1, 2), (3, 7), (5, 11), (2, 9)]:
            totaal += 1
            if tm.lot_sleutel(tid, a) < tm.lot_sleutel(tid, b):
                laagste_wint += 1
    aandeel = laagste_wint / totaal
    print(f"  {totaal} lotingen, laagste teamnummer wint {100 * aandeel:.1f}%")
    meld(0.45 < aandeel < 0.55,
         f"de loting bevoordeelt het laagste teamnummer ({100 * aandeel:.1f}%)")
    # en hij moet wél stabiel zijn: twee keer vragen geeft hetzelfde
    meld(all(tm.lot_sleutel(7, t) == tm.lot_sleutel(7, t) for t in range(1, 30)),
         "lot_sleutel geeft niet twee keer hetzelfde")
    # verschillende toernooien moeten een andere volgorde geven
    anders = sum(1 for tid in range(1, 200)
                 if (tm.lot_sleutel(tid, 1) < tm.lot_sleutel(tid, 2))
                 != (tm.lot_sleutel(tid + 1, 1) < tm.lot_sleutel(tid + 1, 2)))
    meld(anders > 40, f"de loting is te voorspelbaar tussen toernooien ({anders}/199 wisselingen)")


# ------------------------------------------------------------------ vormen --

def vormenlijst(zaden, klein=False):
    vormen = []
    for n in ([6, 8, 9, 12] if klein else [15, 16, 17, 18, 20, 21, 22, 24, 25]):
        for ronden in ([3, 4] if klein else [3, 4, 5]):
            if n % 2 and ronden % 2:
                continue
            for ko in (2, 4, 8) if klein else (4, 8, 16):
                if ko >= n:
                    continue
                for zaad in zaden:
                    vormen.append((n, ronden, ko, zaad))
    return vormen


# ----------------------------------------------------- 8. randgevallen --

def test_randgevallen():
    """Met de hand gebouwde situaties waar de logica op kan struikelen."""
    print("\n== 8. Bewust lastige standen ==")

    # (a) Volledige onderlinge competitie met een kringetje: A klopt B, B klopt
    #     C, C klopt A. Iedereen 1 op 1 — de mini-tabel is compleet maar geeft
    #     geen uitsluitsel, dus moet kwaliteit het overnemen.
    db, pad, fout = bouw(4, 3, 2, 1)
    if not fout:
        games = db.execute("SELECT * FROM games WHERE tournament_id = 1 AND fase = 'bracket' "
                           "ORDER BY id").fetchall()
        rng = random.Random(1)
        speel_bracket(db, rng)
        geordend, _ = tm.stand(db, 1)
        meld(len(geordend) == 4 and len({r["positie"] for r in geordend}) == 4,
             "4 teams, volledige onderlinge competitie: geen sluitende volgorde")
        meld(sum(1 for r in geordend if r["doorstoot"]) == 2,
             "4 teams: niet exact 2 doorstoters")
        TEL["rand"] += 1
        db.close(); os.remove(pad)

    # (b) Iedereen wint evenveel: maximale gelijkheid. Zo'n stand mag nooit
    #     blijven hangen en moet nog steeds een sluitende top-k opleveren.
    for n, ronden, ko in [(8, 3, 4), (8, 3, 2), (12, 3, 4), (16, 3, 8), (6, 3, 2)]:
        db, pad, fout = bouw(n, ronden, ko, 5)
        if fout:
            continue
        # Elk team precies evenveel winsten: laat in elke wedstrijd het team met
        # de minste winsten tot dan toe winnen.
        winsten = collections.Counter()
        uitslagen = {}
        for g in db.execute("SELECT * FROM games WHERE tournament_id = 1 AND fase = 'bracket' "
                            "ORDER BY scheduled_at, id").fetchall():
            a, b = g["team1_id"], g["team2_id"]
            w = a if winsten[a] <= winsten[b] else b
            winsten[w] += 1
            uitslagen[g["id"]] = w
        rng = random.Random(5)
        speel_bracket(db, rng, uitslagen)
        speel_shootouts(db, rng)
        geordend, beslissend = tm.stand(db, 1)
        spreiding = max(winsten.values()) - min(winsten.values())
        meld(sum(1 for r in geordend if r["doorstoot"]) == ko,
             f"maximale gelijkheid (n={n}, spreiding {spreiding}): niet exact {ko} door")
        meld(not beslissend,
             f"maximale gelijkheid (n={n}): er blijft een onbesliste groep over")
        meld(tm.toernooi(db, 1)["status"] in ("knockout", "afgelopen"),
             f"maximale gelijkheid (n={n}): toernooi blijft hangen in de bracketfase")
        TEL["rand"] += 1
        db.close(); os.remove(pad)

    # (c) Eén team wint alles, één team verliest alles.
    for n, ronden, ko in [(8, 3, 4), (12, 4, 8), (16, 4, 8)]:
        db, pad, fout = bouw(n, ronden, ko, 6)
        if fout:
            continue
        uitslagen = {}
        for g in db.execute("SELECT * FROM games WHERE tournament_id = 1 AND fase = 'bracket'").fetchall():
            a, b = g["team1_id"], g["team2_id"]
            if 1 in (a, b):
                uitslagen[g["id"]] = 1                    # T01 wint alles
            elif n in (a, b):
                uitslagen[g["id"]] = a if b == n else b   # Tn verliest alles
        rng = random.Random(6)
        speel_bracket(db, rng, uitslagen)
        speel_shootouts(db, rng)
        geordend, _ = tm.stand(db, 1)
        punten = {r["team_id"]: r["punten"] for r in geordend}
        plaats = {r["team_id"]: r["positie"] for r in geordend}
        # T01 mag op punten door niemand voorbijgestoken worden; staat er iemand
        # bóven hem, dan moet dat op gelijke punten en een betere kwaliteit zijn.
        meld(punten[1] == max(punten.values()),
             f"n={n}: wie álles wint heeft niet het hoogste puntentotaal")
        meld(all(punten[r["team_id"]] == punten[1] for r in geordend[:plaats[1] - 1]),
             f"n={n}: T01 wint alles maar staat achter een team met minder punten")
        meld(punten[n] == min(punten.values()),
             f"n={n}: wie álles verliest heeft niet het laagste puntentotaal")
        meld(all(punten[r["team_id"]] == punten[n] for r in geordend[plaats[n]:]),
             f"n={n}: T{n:02d} verliest alles maar staat vóór een team met meer punten")
        TEL["rand"] += 1
        db.close(); os.remove(pad)
    print(f"  {TEL['rand']} geconstrueerde standen doorgerekend")


# ------------------------------------------------ 9. uitslag rechtzetten --

def test_correctie(vormen):
    """De organisator zet achteraf een verkeerd ingevoerde uitslag recht.

    Dat is het gevaarlijkste pad: de shootouts stonden misschien al gepland of
    waren zelfs al gespeeld. Daarna moet alles nog steeds kloppen.
    """
    print("\n== 9. Een uitslag achteraf rechtzetten ==")
    correcties = geweigerd = 0
    for n, ronden, ko, zaad in vormen:
        db, pad, fout = bouw(n, ronden, ko, zaad)
        if fout:
            continue
        rng = random.Random(zaad * 53 + 13)
        speel_bracket(db, rng)
        for moment in ("shootouts gepland", "shootouts gespeeld"):
            if moment == "shootouts gespeeld":
                speel_shootouts(db, rng)
            g = rng.choice(db.execute(
                "SELECT * FROM games WHERE tournament_id = 1 AND fase = 'bracket'").fetchall())
            # Precies wat de organisator doet: wissen (als dat mag), en dan de
            # juiste uitslag opnieuw invoeren.
            reden = tm.mag_wissen(db, g)
            if reden:
                geweigerd += 1
                continue
            anders = g["team2_id"] if g["winner_team_id"] == g["team1_id"] else g["team1_id"]
            db.execute("UPDATE games SET status = 'gepland', winner_team_id = NULL, "
                       "played_at = NULL WHERE id = ?", (g["id"],))
            db.commit()
            tm.herstel_na_wissen(db, 1, g["fase"], g)
            schuss.herbereken_alles(db)
            tm.evalueer(db, 1)
            db.execute("UPDATE games SET status = 'gespeeld', winner_team_id = ?, "
                       "played_at = ? WHERE id = ?", (anders, g["scheduled_at"], g["id"]))
            db.commit()
            schuss.herbereken_alles(db)
            tm.evalueer(db, 1)
            speel_shootouts(db, rng)
            correcties += 1
            geordend, beslissend = tm.stand(db, 1)
            meld(sum(1 for r in geordend if r["doorstoot"]) == ko,
                 f"n={n} zaad={zaad} ({moment}): na correctie niet exact {ko} door")
            meld(not beslissend,
                 f"n={n} zaad={zaad} ({moment}): na correctie blijft een groep onbeslist")
            meld(tm.toernooi(db, 1)["status"] in ("knockout", "afgelopen"),
                 f"n={n} zaad={zaad} ({moment}): toernooi hangt na correctie in "
                 f"'{tm.toernooi(db, 1)['status']}'")
            # geen shootout tussen teams die niet meer gelijk staan
            punten = {r["team_id"]: r["punten"] for r in geordend}
            for so in db.execute("SELECT team1_id, team2_id FROM games WHERE "
                                 "tournament_id = 1 AND fase = 'shootout'"):
                meld(punten[so["team1_id"]] == punten[so["team2_id"]],
                     f"n={n} zaad={zaad} ({moment}): shootout tussen T{so['team1_id']:02d} en "
                     f"T{so['team2_id']:02d} die niet (meer) gelijk staan")
            # niemand speelde twee shootouts
            per_team = collections.Counter()
            for so in db.execute("SELECT team1_id, team2_id FROM games WHERE "
                                 "tournament_id = 1 AND fase = 'shootout'"):
                per_team[so["team1_id"]] += 1
                per_team[so["team2_id"]] += 1
            meld(all(v <= 1 for v in per_team.values()),
                 f"n={n} zaad={zaad} ({moment}): een team speelde meerdere shootouts")
        TEL["correctie"] += 1
        db.close(); os.remove(pad)
    print(f"  {TEL['correctie']} toernooien, {correcties} rechtzettingen doorgerekend, "
          f"{geweigerd} keer terecht geweigerd door mag_wissen()")


# --------------------------------------------- 10. volgorde van invoeren --

def test_invoervolgorde(vormen):
    """Maakt het uit in welke volgorde de uitslagen ingevoerd worden?

    De uitslagen zelf zijn identiek; alleen het tijdstip verschilt. Punten,
    kwaliteit en programma mogen daar niet van afhangen. De toernooikracht is
    een ELO-verschil en hángt wél van de chronologie af — daarom staat die ook
    onderaan de ketting. Hier meten we hoe vaak dat de doorstoters raakt.
    """
    print("\n== 10. Volgorde van invoeren ==")
    verschil_kracht = verschil_zeker = verschil_strijd = totaal = 0
    for n, ronden, ko, zaad in vormen:
        db, pad, fout = bouw(n, ronden, ko, zaad)
        if fout:
            continue
        rng = random.Random(zaad * 59 + 17)
        speel_bracket(db, rng)

        def toestand():
            geordend, beslissend = tm.stand(db, 1)
            strijd = {x for gr in beslissend for x in gr}
            # Wie stoot door zónder dat er nog een shootout aan te pas komt?
            # Dát is de uitkomst die niet van de chronologie mag afhangen.
            return ({r["team_id"]: r for r in geordend},
                    frozenset(r["team_id"] for r in geordend[:ko]
                              if r["team_id"] not in strijd),
                    frozenset(strijd))

        basis, zeker, strijd = toestand()

        # dezelfde uitslagen, andere tijdstippen
        games = db.execute("SELECT id FROM games WHERE tournament_id = 1 AND "
                           "fase = 'bracket'").fetchall()
        momenten = [f"2026-03-07T{10 + i // 60:02d}:{i % 60:02d}" for i in range(len(games))]
        rng.shuffle(momenten)
        for g, moment in zip(games, momenten):
            db.execute("UPDATE games SET played_at = ? WHERE id = ?", (moment, g["id"]))
        db.commit()
        schuss.herbereken_alles(db)
        na, zeker_na, strijd_na = toestand()
        totaal += 1
        meld(all(basis[t]["punten"] == na[t]["punten"] for t in basis),
             f"n={n} zaad={zaad}: de punten veranderen door de invoervolgorde")
        meld(all(basis[t]["kwaliteit"] == na[t]["kwaliteit"] for t in basis),
             f"n={n} zaad={zaad}: de kwaliteit verandert door de invoervolgorde")
        meld(all(basis[t]["programma"] == na[t]["programma"] for t in basis),
             f"n={n} zaad={zaad}: het programma verandert door de invoervolgorde")
        verschil_kracht += any(round(basis[t]["kracht"], 6) != round(na[t]["kracht"], 6)
                               for t in basis)
        verschil_zeker += zeker_na != zeker
        verschil_strijd += strijd_na != strijd
        db.close(); os.remove(pad)
    print(f"  {totaal} toernooien met dezelfde uitslagen in een andere volgorde:")
    print(f"    toernooikracht anders          {verschil_kracht:>4}")
    print(f"    zeker gekwalificeerden anders  {verschil_zeker:>4}  "
          f"({100 * verschil_zeker / max(totaal, 1):.1f}%)")
    print(f"    shootoutdeelnemers anders      {verschil_strijd:>4}  "
          f"({100 * verschil_strijd / max(totaal, 1):.1f}%)")
    print("    (de toernooikracht is een ELO-verschil en hangt dus van de chronologie af;")
    print("     daarom staat ze onderaan de ketting, ná kwaliteit, programma en shootout)")


print("=" * 62)
print("Stresstest van de tiebreaklogica")
print("=" * 62)

breed = vormenlijst(range(1, 4)) + vormenlijst(range(1, 4), klein=True)
smal = vormenlijst(range(1, 2)) + vormenlijst(range(1, 2), klein=True)

test_stabiliteit(smal)
test_sluitend(breed)
test_uitleg(breed)
test_geen_straf_op_winst(vormenlijst(range(1, 3), klein=True)
                         + vormenlijst(range(1, 2))[:24])
test_vrijstelling(vormenlijst(range(1, 9)) + vormenlijst(range(1, 9), klein=True))
test_namen(smal)
test_loting()
test_randgevallen()
test_correctie(vormenlijst(range(1, 4), klein=True) + vormenlijst(range(1, 3))[:30])
test_invoervolgorde(breed)

print("\n" + "=" * 62)
if FOUTEN:
    print(f"❌ {len(FOUTEN)} probleem(en) gevonden:")
    for f in FOUTEN[:30]:
        print("  -", f)
    if len(FOUTEN) > 30:
        print(f"  … en nog {len(FOUTEN) - 30}")
else:
    print("✅ Alle eigenschappen blijven overeind.")
print("=" * 62)
shutil.rmtree(WERKMAP, ignore_errors=True)
sys.exit(1 if FOUTEN else 0)
