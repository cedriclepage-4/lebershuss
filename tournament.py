# -*- coding: utf-8 -*-
"""
Toernooimotor van Leberschuss Tonzent.

Formaat (naar het model van de nieuwe Champions League):

1. **Bracketfase** — álle teams zitten in één grote bracket. De organisator kiest
   hoeveel wedstrijden elk team speelt. De tegenstanders worden geloot uit de
   potten (pot 1 = sterkste teams volgens permanente Aura), zodat elk team een
   evenwichtig programma krijgt en niemand twee keer dezelfde tegenstander loot.
2. **Shootouts** — staan er na de bracketfase teams gelijk op punten én beslist
   dat over een knockoutticket, dan genereert het toernooi automatisch extra
   beslissingswedstrijden. Het onderlinge resultaat telt enkel als álle betrokken
   teams onderling gespeeld hebben (anders vergelijk je ongelijke steekproeven);
   in alle andere gevallen volgt een shootout — winnen of verliezen, gelijk bestaat
   niet. Een shootout beslist enkel wie doorstoot en telt niet mee voor de Aura.
3. **Knockout** — de beste 2, 4, 8, 16, ... teams gaan door. Nummer 1 speelt
   tegen de laagste geplaatste, enzovoort, zodat de nummers 1 en 2 elkaar pas in
   de finale kunnen tegenkomen.

De kalender houdt rekening met de beschikbare locaties ("tafel 1", "tuintafel", ...):
per ronde spelen er nooit meer wedstrijden tegelijk dan er tafels zijn, en een
team staat nooit op twee tafels tegelijk.
"""

import hashlib
import math
import random
from datetime import datetime, timedelta

PUNTEN_WINST = 3
TIJDFORMAAT = "%Y-%m-%dT%H:%M"

WEERGAVE = "COALESCE(NULLIF(p.nickname, ''), p.name)"


# ------------------------------------------------------------------ ophalen --

def toernooi(db, tid):
    return db.execute("SELECT * FROM tournaments WHERE id = ?", (tid,)).fetchone()


def locaties(db, tid):
    return db.execute("SELECT * FROM tournament_locations WHERE tournament_id = ? "
                      "ORDER BY id", (tid,)).fetchall()


def deelnemers(db, tid, ook_teruggetrokken=False):
    """Teams van dit toernooi, met naam, permanente Aura en de twee spelers.

    Wie zich teruggetrokken heeft, doet niet meer mee: die valt uit de stand, uit
    de loting en uit alle berekeningen.
    """
    rijen = db.execute(f"""
        SELECT tt.team_id AS id, tt.pot, tt.seed, tt.start_elo, tt.teruggetrokken,
               t.name AS naam, t.elo, t.avatar,
               t.player1_id, t.player2_id
        FROM tournament_teams tt
        JOIN teams t ON t.id = tt.team_id
        WHERE tt.tournament_id = ?
          {"" if ook_teruggetrokken else "AND tt.teruggetrokken = 0"}
        ORDER BY t.elo DESC, t.name
    """, (tid,)).fetchall()
    namen = {r["id"]: r["naam"] for r in db.execute(
        f"SELECT p.id, {WEERGAVE} AS naam FROM players p")}
    uit = []
    for r in rijen:
        d = dict(r)
        d["spelers"] = f'{namen.get(r["player1_id"], "?")} & {namen.get(r["player2_id"], "?")}'
        uit.append(d)
    return uit


def games_van(db, tid, fase=None):
    sql = "SELECT * FROM games WHERE tournament_id = ?"
    args = [tid]
    if fase:
        sql += " AND fase = ?"
        args.append(fase)
    sql += " ORDER BY COALESCE(ronde, 0), positie, scheduled_at, id"
    return db.execute(sql, args).fetchall()


# --------------------------------------------------------------- hulpmiddel --

def is_macht_van_twee(n):
    return n >= 2 and (n & (n - 1)) == 0


def seed_volgorde(n):
    """Klassieke bracketvolgorde: [1, n, n/2+... ] zodat 1 en 2 elkaar pas in de
    finale kunnen treffen. Voor n=8: [1, 8, 4, 5, 2, 7, 3, 6]."""
    orde = [1, 2]
    while len(orde) < n:
        m = len(orde) * 2 + 1
        nieuw = []
        for s in orde:
            nieuw.extend([s, m - s])
        orde = nieuw
    return orde


def lot_sleutel(tid, team_id):
    """Een vaste, willekeurig ogende waarde per team binnen één toernooi.

    Nodig om teams te ordenen die écht niet van elkaar te onderscheiden zijn —
    bijvoorbeeld op een eerste toernooi, wanneer iedereen nog op 1000 Aura staat.
    Zonder dit zou de alfabetische naam beslissen: “Team A” zou dan altijd boven
    “Team Z” eindigen, wat systematisch oneerlijk is.

    Het is met opzet gebaseerd op het toernooinummer én het teamnummer: dezelfde
    volgorde bij elke herberekening (anders zou de stand blijven verspringen),
    maar een andere volgorde in een volgend toernooi.
    """
    ruw = hashlib.sha1(f"{tid}:{team_id}".encode()).hexdigest()
    return int(ruw[:12], 16)


# Vier potten. Meer levert bij deze aantallen niets extra's op en minder maakt
# het onderscheid grof; het is bovendien één instelling minder om over na te
# denken. Staat iedereen op dezelfde rating, dan vervallen de potten sowieso.
STANDAARD_POTTEN = 4


def potten_zinvol(teams):
    """Hebben potten hier betekenis?

    Potten bestaan om de sterke teams uit elkaar te loten. Staat iedereen op
    exact dezelfde rating, dan valt er niets te spreiden en is "pot 1" niet meer
    dan een etiket. Dan slaan we ze over.
    """
    return len({round(t["elo"], 6) for t in teams}) > 1


def potten_aantal(db, tid):
    """Over hoeveel potten is dit toernooi geloot? 1 (of 0) = geen potten."""
    potten = {r["pot"] for r in db.execute(
        "SELECT pot FROM tournament_teams WHERE tournament_id = ?", (tid,))}
    return len(potten - {None})


def potten_verdelen(teams, aantal_potten, rng=None):
    """Verdeel de teams (gesorteerd op permanente Aura) over de potten.

    Geeft {team_id: potnummer} terug; pot 1 bevat de sterkste teams. Teams met
    exact dezelfde Aura worden door elkaar geschud: op een eerste toernooi staat
    iedereen op 1000, en dan mag de alfabetische volgorde niet bepalen wie in
    welke pot belandt.
    """
    n = len(teams)
    aantal_potten = max(1, min(aantal_potten, n))
    teams = list(teams)
    if rng is not None:
        rng.shuffle(teams)          # sorted() is stabiel: gelijke Aura blijft geschud
    gesorteerd = sorted(teams, key=lambda t: -t["elo"])
    basis, rest = divmod(n, aantal_potten)
    pot_van = {}
    i = 0
    for p in range(aantal_potten):
        grootte = basis + (1 if p < rest else 0)
        for t in gesorteerd[i:i + grootte]:
            pot_van[t["id"]] = p + 1
        i += grootte
    return pot_van


# ----------------------------------------------------------------- loting --

def _ronde_matching(team_ids, verboden, pot_van, teller, rng, pogingen=600):
    """Eén ronde: koppel alle teams twee aan twee.

    * nooit twee keer dezelfde affiche (`verboden`)
    * bij voorkeur tegen een pot waartegen je nog weinig speelde (`teller`)
    """
    for _ in range(pogingen):
        rest = list(team_ids)
        rng.shuffle(rest)
        paren = []
        gelukt = True
        while rest:
            a = rest.pop(0)
            kandidaten = [b for b in rest
                          if (a, b) not in verboden and (b, a) not in verboden]
            if not kandidaten:
                gelukt = False
                break
            kandidaten.sort(key=lambda b: (teller[(a, pot_van[b])]
                                           + teller[(b, pot_van[a])], rng.random()))
            beste = kandidaten[0]
            rest.remove(beste)
            paren.append((a, beste))
        if gelukt:
            return paren
    return None


def _regulier_loten(team_ids, pot_van, ronden, rng, pogingen=800):
    """Loot bij een ONEVEN aantal teams.

    Een ronde waarin iedereen tegelijk speelt, bestaat dan niet. In plaats
    daarvan loten we gewoon de affiches zelf: elk team krijgt exact `ronden`
    tegenstanders, allemaal verschillend, zo veel mogelijk gespreid over de
    potten. De kalender verdeelt die wedstrijden nadien over de speelrondes,
    waarbij elk team één ronde rust. Iedereen speelt dus evenveel wedstrijden.
    """
    for _ in range(pogingen):
        rest = {t: ronden for t in team_ids}
        teller = {(t, p): 0 for t in team_ids for p in set(pot_van.values())}
        paren = []
        gelukt = True
        while any(v > 0 for v in rest.values()):
            # Begin bij het team met de meeste openstaande wedstrijden.
            a = max((t for t in team_ids if rest[t] > 0),
                    key=lambda t: (rest[t], rng.random()))
            kandidaten = [b for b in team_ids
                          if b != a and rest[b] > 0
                          and tuple(sorted((a, b))) not in paren]
            if not kandidaten:
                gelukt = False
                break
            kandidaten.sort(key=lambda b: (teller[(a, pot_van[b])]
                                           + teller[(b, pot_van[a])],
                                           -rest[b], rng.random()))
            b = kandidaten[0]
            paren.append(tuple(sorted((a, b))))
            rest[a] -= 1
            rest[b] -= 1
            teller[(a, pot_van[b])] += 1
            teller[(b, pot_van[a])] += 1
        if gelukt:
            rng.shuffle(paren)
            return paren
    raise ValueError("De loting lukte niet; probeer een ander aantal wedstrijden per team.")


def _magere_ronde_naar_het_midden(rondes):
    """Schuif de minst gevulde speelrondes naar het midden van het programma.

    Loopt het aantal wedstrijden niet gelijk op met het aantal tafels — bij een
    oneven aantal teams gebeurt dat bijna altijd — dan blijft er ergens een tafel
    vrij. Vooraan is dat het vervelendst: dan staat een deel van de zaal bij de
    aftrap al te wachten terwijl iedereen er net zin in heeft. Achteraan valt het
    ook op, vlak vóór de knockout. In het midden is het gewoon een adempauze.

    De volle rondes houden onderling hun oorspronkelijke volgorde, zodat de
    spreiding die hierboven berekend is (niemand twee keer vlak na elkaar) zoveel
    mogelijk overeind blijft; enkel het magere blok verhuist.
    """
    if len({len(r) for r in rondes}) < 2:
        return rondes                       # allemaal even vol: niets te schuiven
    volst = max(len(r) for r in rondes)
    vol = [r for r in rondes if len(r) == volst]
    mager = sorted((r for r in rondes if len(r) < volst), key=lambda r: -len(r))

    def achter_elkaar(reeks):
        """Hoe vaak moet een team twee speelrondes na elkaar aantreden?"""
        bezet = [{t for paar in r for t in paar} for r in reeks]
        return sum(len(a & b) for a, b in zip(bezet, bezet[1:]))

    # Zo centraal mogelijk invoegen, en minstens één volle ronde vooraan — ook
    # als er maar weinig volle rondes zijn. Liggen twee plekken even centraal
    # (bij een oneven aantal volle rondes), dan kiezen we die waarbij de minste
    # teams twee speelrondes na elkaar moeten aantreden.
    doel = (len(vol) + len(mager) - 1) / 2          # het midden van het programma
    kandidaten = range(1, max(2, len(vol) + 1))

    def score(k):
        hart_van_het_blok = k + (len(mager) - 1) / 2
        return (abs(hart_van_het_blok - doel),
                achter_elkaar(vol[:k] + mager + vol[k:]))

    beste = min(kandidaten, key=score)
    return vol[:beste] + mager + vol[beste:]


def _past_op_de_tafels(rondes, plafond):
    """Kan dit schema zo gespeeld worden: genoeg tafels, niemand twee keer tegelijk?"""
    for ronde in rondes:
        if len(ronde) > plafond:
            return False
        teams = [t for paar in ronde for t in paar]
        if len(set(teams)) != len(teams):
            return False
    return True


def _beste_van(gevonden, voorstel, plafond):
    """Kies tussen wat de zoektocht vond en de ronden van de loting zelf."""
    if (voorstel and len(voorstel) < len(gevonden)
            and _past_op_de_tafels(voorstel, plafond)):
        return _magere_ronde_naar_het_midden([list(r) for r in voorstel])
    return _magere_ronde_naar_het_midden(gevonden)


def verdeel_in_rondes(paren, rng=None, max_per_ronde=None, pogingen=150, voorstel=None):
    """Verdeel alle affiches over zo weinig mogelijk speelrondes.

    Een speelronde is één tijdstip: alle wedstrijden erin worden tegelijk
    gespeeld, elk op een eigen tafel. Voorwaarden:

    * geen enkel team staat twee keer in dezelfde speelronde;
    * er staan nooit meer wedstrijden in een ronde dan er tafels zijn.

    Er wordt niet per "wedstrijdronde" gepland maar over het hele toernooi heen:
    zit er in de eerste ronde een tafel vrij, dan schuift er meteen een wedstrijd
    van verderop in het programma in. Zo staat geen enkele tafel stil.
    """
    rng = rng or random.Random()
    if not paren:
        return []
    teams = {t for paar in paren for t in paar}
    plafond = max_per_ronde or max(1, len(teams) // 2)
    plafond = max(1, min(plafond, len(teams) // 2))
    graad = max(sum(1 for paar in paren if t in paar) for t in teams)
    minimaal = max(graad, math.ceil(len(paren) / plafond))

    # De loting heeft de wedstrijden al in ronden gezet waarin elk team precies
    # één keer speelt. Zijn er genoeg tafels voor zo'n ronde, dan is dat meteen
    # het best denkbare schema: iedereen speelt elke ronde, niemand zit te
    # wachten. Opnieuw gaan puzzelen kan er dan enkel slechter van worden — en
    # dat gebeurde ook: 14 teams op 7 tafels werden 6 speelrondes in plaats van 5.
    if voorstel and len(voorstel) <= minimaal and _past_op_de_tafels(voorstel, plafond):
        return _magere_ronde_naar_het_midden([list(r) for r in voorstel])

    for aantal in range(minimaal, len(paren) + 1):
        beste = None
        for _ in range(pogingen):
            volgorde = list(paren)
            rng.shuffle(volgorde)
            # Teams met veel wedstrijden eerst: die zijn het moeilijkst te plaatsen.
            druk = {t: sum(1 for paar in paren if t in paar) for t in teams}
            volgorde.sort(key=lambda p: -(druk[p[0]] + druk[p[1]]))
            rondes = [[] for _ in range(aantal)]
            bezet = [set() for _ in range(aantal)]
            gelukt = True
            for a, b in volgorde:
                kandidaten = [i for i in range(aantal)
                              if len(rondes[i]) < plafond
                              and a not in bezet[i] and b not in bezet[i]]
                if not kandidaten:
                    gelukt = False
                    break
                # Liefst een lege ronde, en liefst niet pal na een vorige wedstrijd
                # van dezelfde teams (even op adem komen).
                def kost(k):
                    rust = sum(1 for buur in (k - 1, k + 1)
                               if 0 <= buur < aantal and (bezet[buur] & {a, b}))
                    return (len(rondes[k]), rust, k)
                i = min(kandidaten, key=kost)
                rondes[i].append((a, b))
                bezet[i].update((a, b))
            if gelukt:
                gevuld = [r for r in rondes if r]
                if beste is None or len(gevuld) < len(beste):
                    beste = gevuld
                if len(gevuld) == aantal:
                    return _beste_van(gevuld, voorstel, plafond)
        if beste:
            return _beste_van(beste, voorstel, plafond)
    return [[paar] for paar in paren]        # noodoplossing: alles apart


def loot_rondes(team_ids, pot_van, ronden, rng=None):
    """Loot de affiches van de bracketfase; elk team speelt `ronden` wedstrijden.

    Bij een even aantal teams levert dat volle speelrondes op (iedereen speelt
    elke ronde). Bij een oneven aantal rust er elke ronde één team, maar speelt
    iedereen wél evenveel wedstrijden.
    """
    rng = rng or random.Random()
    if ronden > len(team_ids) - 1:
        raise ValueError("Er zijn te weinig teams voor zoveel wedstrijden per team.")
    if (len(team_ids) * ronden) % 2:
        raise ValueError("Met een oneven aantal teams moet het aantal wedstrijden "
                         "per team even zijn.")
    if len(team_ids) % 2:
        return verdeel_in_rondes(_regulier_loten(team_ids, pot_van, ronden, rng), rng)

    for _ in range(120):                       # volledige loting opnieuw proberen
        verboden = set()
        teller = {}
        for t in team_ids:
            for p in set(pot_van.values()):
                teller[(t, p)] = 0
        rondes = []
        gelukt = True
        for _r in range(ronden):
            paren = _ronde_matching(team_ids, verboden, pot_van, teller, rng)
            if paren is None:
                gelukt = False
                break
            for a, b in paren:
                verboden.add((a, b))
                teller[(a, pot_van[b])] += 1
                teller[(b, pot_van[a])] += 1
            rondes.append(paren)
        if gelukt:
            return rondes
    raise ValueError("De loting lukte niet; probeer minder wedstrijden per team.")


# ---------------------------------------------------------------- kalender --

def _plan(rondes, tafel_ids, start, slot_minuten):
    """Zet elke wedstrijd op een tijdstip en een tafel. Geeft per ronde een lijst
    van (team_a, team_b, tijdstip, locatie_id)."""
    tafels = len(tafel_ids)
    resultaat = []
    tijd = start
    for paren in rondes:
        golven = math.ceil(len(paren) / tafels) if tafels else 1
        gepland = []
        for i, (a, b) in enumerate(paren):
            golf = i // tafels if tafels else 0
            loc = tafel_ids[i % tafels] if tafels else None
            gepland.append((a, b, tijd + timedelta(minutes=slot_minuten * golf), loc))
        resultaat.append(gepland)
        tijd = tijd + timedelta(minutes=slot_minuten * golven)
    return resultaat, tijd


def _starttijd(t):
    try:
        return datetime.fromisoformat(f'{t["date"]}T{t["start_tijd"]}')
    except ValueError:
        return datetime.fromisoformat(f'{t["date"]}T19:00')


def _laatste_moment(db, tid, standaard):
    rij = db.execute("SELECT MAX(scheduled_at) AS m FROM games WHERE tournament_id = ?",
                     (tid,)).fetchone()
    if not rij or not rij["m"]:
        return standaard
    try:
        return datetime.fromisoformat(rij["m"])
    except ValueError:
        return standaard


# --------------------------------------------------------------- genereren --

def controleer(db, tid):
    """Controleer of het toernooi gegenereerd kan worden. Geeft een foutmelding
    terug, of None als alles in orde is."""
    t = toernooi(db, tid)
    if not t:
        return "Toernooi niet gevonden."
    if t["status"] != "opzet":
        return "Dit toernooi is al gegenereerd."
    teams = deelnemers(db, tid)
    n = len(teams)
    r = t["bracket_ronden"]
    if n < 4:
        return "Voeg minstens 4 teams toe."
    if n % 2 and r % 2:
        opties = " of ".join(str(x) for x in (r - 1, r + 1) if 1 <= x <= n - 1)
        return (f"Met {n} teams (een oneven aantal) moet elk team een even aantal "
                f"wedstrijden spelen, anders kan niet iedereen er evenveel afwerken. "
                f"Kies er {opties} in plaats van {r}.")
    if not is_macht_van_twee(t["ko_teams"]):
        return "Het aantal teams dat doorstoot moet 2, 4, 8, 16, ... zijn."
    if t["ko_teams"] >= n:
        return (f"Er stoten {t['ko_teams']} teams door terwijl er maar {n} meedoen. "
                "Kies een kleiner knockoutschema.")
    if t["bracket_ronden"] < 1 or t["bracket_ronden"] > n - 1:
        return f"Kies tussen 1 en {n - 1} wedstrijden per team in de bracketfase."
    # Spelers mogen maar in één team van hetzelfde toernooi zitten.
    gezien = {}
    for team in teams:
        for pid in (team["player1_id"], team["player2_id"]):
            if pid in gezien:
                return (f"“{team['naam']}” en “{gezien[pid]}” delen een speler. "
                        "Elke speler mag maar in één team per toernooi zitten.")
            gezien[pid] = team["naam"]
    return None


def genereer(db, tid, rng=None):
    """Loot de potten, de bracketfase en de kalender. Geeft (ok, boodschap)."""
    fout = controleer(db, tid)
    if fout:
        return False, fout

    t = toernooi(db, tid)
    teams = deelnemers(db, tid)
    # Staat iedereen op dezelfde rating — het geval bij een eerste toernooi — dan
    # zeggen potten niets: elke indeling is dan even willekeurig. We loten dan
    # gewoon vrij en laten de potten uit beeld, in plaats van de zaal een
    # rangschikking voor te schotelen die nergens op slaat.
    aantal_potten = STANDAARD_POTTEN if potten_zinvol(teams) else 1
    pot_van = potten_verdelen(teams, aantal_potten, rng)
    rng = rng or random.Random()

    try:
        rondes = loot_rondes([x["id"] for x in teams], pot_van,
                             t["bracket_ronden"], rng)
    except ValueError as e:
        return False, str(e)

    tafel_ids = [l["id"] for l in locaties(db, tid)]
    # De loting bepaalt wélke wedstrijden er zijn; de kalender plant ze daarna over
    # het hele toernooi heen in, zodat er nooit een tafel stilstaat.
    alle_paren = [paar for ronde in rondes for paar in ronde]
    speelrondes = verdeel_in_rondes(alle_paren, rng,
                                    max_per_ronde=len(tafel_ids) or None,
                                    voorstel=rondes)
    gepland, _einde = _plan(speelrondes, tafel_ids, _starttijd(t), t["slot_minuten"])

    db.execute("DELETE FROM games WHERE tournament_id = ?", (tid,))
    for team in teams:
        db.execute("UPDATE tournament_teams SET pot = ?, start_elo = ? "
                   "WHERE tournament_id = ? AND team_id = ?",
                   (pot_van[team["id"]], team["elo"], tid, team["id"]))
    for nummer, paren in enumerate(gepland, start=1):
        for a, b, moment, loc in paren:
            db.execute("""
                INSERT INTO games (team1_id, team2_id, tournament_id, fase, ronde,
                                   scheduled_at, location_id)
                VALUES (?, ?, ?, 'bracket', ?, ?, ?)
            """, (a, b, tid, nummer, moment.strftime(TIJDFORMAAT), loc))
    db.execute("UPDATE tournaments SET status = 'bracket' WHERE id = ?", (tid,))
    db.commit()
    aantal = sum(len(p) for p in gepland)
    duur = len(gepland) * t["slot_minuten"]
    return True, (f"Toernooi geloot: {len(teams)} teams, {t['bracket_ronden']} "
                  f"wedstrijden per team, {aantal} wedstrijden in de bracketfase "
                  f"verdeeld over {len(gepland)} speelrondes "
                  f"(± {duur // 60}u{duur % 60:02d} met "
                  f"{len(tafel_ids) or 'onbeperkt'} tafel(s)).")


def _aanvulparen(deficit, ontmoet, rng, pogingen=4000):
    """Zoek nieuwe affiches zodat elk team zijn tekort precies wegwerkt.

    `deficit` = {team: aantal wedstrijden dat het nog moet spelen}
    `ontmoet` = {team: set van teams waartegen het al speelde}

    Niemand speelt twee keer dezelfde tegenstander. Het moeilijkste team eerst
    koppelen (dat met het grootste tekort) — anders blijft er op het einde een
    ploeg over die enkel nog tegen zichzelf zou kunnen.
    """
    if sum(deficit.values()) % 2:
        return None
    for _ in range(pogingen):
        rest = dict(deficit)
        al = {t: set(v) for t, v in ontmoet.items()}
        paren = []
        gelukt = True
        while any(rest.values()):
            a = max(rest, key=lambda t: (rest[t], rng.random()))
            if rest[a] == 0:
                break
            kandidaten = [b for b in rest
                          if b != a and rest[b] > 0 and b not in al[a]]
            if not kandidaten:
                gelukt = False
                break
            b = max(kandidaten, key=lambda t: (rest[t], rng.random()))
            paren.append((a, b))
            al[a].add(b)
            al[b].add(a)
            rest[a] -= 1
            rest[b] -= 1
        if gelukt and not any(rest.values()):
            return paren
    return None


def _terugtrek_situatie(db, tid, blijvers, bracket, bevries_lopende):
    """Wat ligt er vast als deze ploeg wegvalt, en wie speelde al tegen wie?"""
    open_slots = sorted({g["scheduled_at"] for g in bracket if g["status"] == "gepland"})
    huidige = open_slots[0] if open_slots else None
    vast = [g for g in bracket
            if g["status"] == "gespeeld"
            or (bevries_lopende and g["scheduled_at"] == huidige)]
    ontmoet = {b: set() for b in blijvers}
    aantal = {b: 0 for b in blijvers}
    for g in vast:
        a, b = g["team1_id"], g["team2_id"]
        if a in ontmoet and b in ontmoet:
            ontmoet[a].add(b)
            ontmoet[b].add(a)
            aantal[a] += 1
            aantal[b] += 1
    return {g["id"] for g in vast}, ontmoet, aantal, max(aantal.values(), default=0)


def terugtrek_opties(db, tid, team_id):
    """Op hoeveel wedstrijden per team kan je uitkomen als deze ploeg wegvalt?

    Het totaal aantal wedstrijden moet even zijn, dus met een oneven aantal
    overblijvende ploegen kan enkel een even aantal wedstrijden per team — bij
    13 ploegen dus 4 of 6, nooit 5. Blijft er een even aantal ploegen over, dan
    kan het aantal gelijk blijven of er eentje bij.

    Elke optie wordt echt doorgerekend: wat hier in de lijst staat, kan ook.
    De ronde die op dat moment gespeeld wordt, blijft altijd staan: een lopende
    wedstrijd afbreken doen we niet. Past er daardoor geen enkel schema meer,
    dan is de lijst leeg en kan er niet meer teruggetrokken worden.
    """
    t = toernooi(db, tid)
    if not t or t["status"] != "bracket":
        return [], False
    blijvers = [d["id"] for d in deelnemers(db, tid) if d["id"] != team_id]
    if len(blijvers) < 4:
        return [], False
    bracket = [g for g in db.execute(
        "SELECT * FROM games WHERE tournament_id = ? AND fase = 'bracket' "
        "ORDER BY scheduled_at, id", (tid,))
        if team_id not in (g["team1_id"], g["team2_id"])]
    m = len(blijvers)
    tafels = max(1, len(locaties(db, tid)))

    # De ronde die nu op tafel ligt blijft altijd staan. Een lopende wedstrijd
    # afbreken is het ergste wat je kan doen: die mensen staan te spelen. Komt
    # het daarmee niet uit, dan is terugtrekken gewoon geen optie meer — in de
    # laatste ronde weet je toch al lang dat die ploeg niet meer komt.
    _, ontmoet, aantal, ondergrens = _terugtrek_situatie(db, tid, blijvers,
                                                         bracket, True)
    opties = []
    for doel in range(max(ondergrens, t["bracket_ronden"] - 1), t["bracket_ronden"] + 2):
        if doel > m - 1 or (m * doel) % 2:
            continue
        deficit = {b: doel - aantal[b] for b in blijvers}
        if any(v < 0 for v in deficit.values()):
            continue
        nieuw = ([] if not any(deficit.values())
                 else _aanvulparen(deficit, ontmoet, random.Random(doel), pogingen=800))
        if nieuw is None:
            continue
        opties.append({
            "doel": doel,
            "nieuw": len(nieuw),
            "slots": math.ceil(len(nieuw) / tafels),
            "minuten": math.ceil(len(nieuw) / tafels) * t["slot_minuten"],
            "verschil": doel - t["bracket_ronden"],
        })
    if not opties:
        return []
    # Voorkeur: de avond niet langer maken, en zo dicht mogelijk bij wat
    # aangekondigd was.
    beste = min(opties, key=lambda o: (o["verschil"] > 0, abs(o["verschil"])))
    for o in opties:
        o["standaard"] = (o is beste)
    return opties


def terugtrekken(db, tid, team_id, doel=None, rng=None):
    """Een team komt niet (meer) opdagen: schrap het en loot de rest opnieuw.

    Een forfaitzege is geen echte zege. Wie toevallig tegen de afwezige ploeg
    geloot was, kreeg drie punten cadeau; wie een zwaar programma had, niet. Dat
    scheelt genoeg om over de streep te beslissen, dus lossen we het op door
    iedereen even veel échte wedstrijden te laten spelen.

    Wat er gebeurt:
      * alle wedstrijden van de afwezige ploeg vervallen — ook de forfaits die al
        ingevuld waren, want anders houdt wie vroeg tegen hen speelde een
        voordeel op wie laat tegen hen zou spelen;
      * gespeelde wedstrijden en de ronde die nu bezig is blijven staan;
      * alle latere affiches worden opnieuw geloot, zodat elk overblijvend team
        op hetzelfde aantal wedstrijden uitkomt.

    Dat aantal kan niet altijd gelijk blijven: met 13 ploegen die elk 5 partijen
    spelen zou je 32,5 wedstrijden nodig hebben. Dan zakt het doel met één.

    Geeft (gelukt, boodschap) terug.
    """
    rng = rng or random.Random()
    t = toernooi(db, tid)
    if not t:
        return False, "Toernooi niet gevonden."
    if t["status"] not in ("opzet", "bracket"):
        return False, ("De bracketfase is al afgelopen. Terugtrekken kan enkel "
                       "zolang er nog groepswedstrijden op het programma staan.")
    rij = db.execute("SELECT * FROM tournament_teams WHERE tournament_id = ? AND "
                     "team_id = ?", (tid, team_id)).fetchone()
    if not rij:
        return False, "Dat team doet niet mee aan dit toernooi."
    if rij["teruggetrokken"]:
        return False, "Dat team is al teruggetrokken."

    blijvers = [d["id"] for d in deelnemers(db, tid) if d["id"] != team_id]
    if len(blijvers) < 4:
        return False, "Dan blijven er te weinig teams over om verder te spelen."

    # Welk aantal wedstrijden per team wordt het? De organisator mag kiezen;
    # zonder keuze houden we het zo dicht mogelijk bij wat aangekondigd was.
    if t["status"] == "bracket":
        opties = terugtrek_opties(db, tid, team_id)
        if not opties:
            return False, ("Hiervoor is het te laat: er valt geen eerlijk schema meer te "
                           "maken zonder de wedstrijden die nu bezig zijn af te breken. "
                           "Laat de laatste ronde gewoon uitspelen; de forfaits blijven "
                           "staan zoals ze zijn.")
        haalbaar = [o["doel"] for o in opties]
        if doel is None:
            doel = next(o["doel"] for o in opties if o["standaard"])
        elif doel not in haalbaar:
            return False, (f"{doel} wedstrijden per team kan hier niet. Mogelijk is: "
                           + " of ".join(str(x) for x in haalbaar) + ".")

    # 1. De afwezige ploeg valt volledig weg.
    db.execute("UPDATE tournament_teams SET teruggetrokken = 1 WHERE tournament_id = ? "
               "AND team_id = ?", (tid, team_id))
    db.execute("DELETE FROM game_reports WHERE game_id IN (SELECT id FROM games "
               "WHERE tournament_id = ? AND (team1_id = ? OR team2_id = ?))",
               (tid, team_id, team_id))
    db.execute("DELETE FROM games WHERE tournament_id = ? AND (team1_id = ? OR team2_id = ?)",
               (tid, team_id, team_id))
    db.execute("DELETE FROM games WHERE tournament_id = ? AND fase IN ('shootout','knockout')",
               (tid,))
    db.execute("UPDATE tournaments SET status = 'bracket' WHERE id = ?", (tid,))
    db.commit()

    if t["status"] == "opzet":
        return True, "Het team is verwijderd; het toernooi was nog niet geloot."

    # 2. Wat ligt vast? Alles wat al gespeeld is, en liefst ook de ronde die nu
    #    op tafel ligt — die wil je niet onder de spelers vandaan trekken. Lukt
    #    het daarmee niet (bv. als de ploeg pas in de làatste ronde afhaakt),
    #    dan geven we die lopende ronde alsnog vrij: beter een affiche die
    #    verandert dan een avond die vastloopt.
    bracket = db.execute("SELECT * FROM games WHERE tournament_id = ? AND fase = 'bracket' "
                         "ORDER BY scheduled_at, id", (tid,)).fetchall()

    vast_ids, ontmoet, aantal, ondergrens = _terugtrek_situatie(
        db, tid, blijvers, bracket, True)
    if doel < ondergrens:
        db.rollback()
        return False, "Er zijn al teams die meer wedstrijden gespeeld hebben dan dat."
    later = [g for g in bracket if g["id"] not in vast_ids]

    # 4. De latere affiches vervallen; er komt een nieuwe loting voor in de plaats.
    for g in later:
        db.execute("DELETE FROM game_reports WHERE game_id = ?", (g["id"],))
        db.execute("DELETE FROM games WHERE id = ?", (g["id"],))
    db.commit()

    deficit = {b: doel - aantal[b] for b in blijvers}
    nieuw = _aanvulparen(deficit, ontmoet, rng) if any(deficit.values()) else []
    if nieuw is None:
        db.rollback()
        return False, ("Er is geen schema te vinden waarin iedereen even veel speelt "
                       "zonder dezelfde tegenstander twee keer te treffen.")

    # 5. Inplannen ná wat er al staat.
    if nieuw:
        tafel_ids = [l["id"] for l in locaties(db, tid)]
        start = _laatste_moment(db, tid, _starttijd(t)) + timedelta(minutes=t["slot_minuten"])
        speelrondes = verdeel_in_rondes(nieuw, rng, max_per_ronde=len(tafel_ids) or None)
        gepland, _ = _plan(speelrondes, tafel_ids, start, t["slot_minuten"])
        volgnr = (db.execute("SELECT COALESCE(MAX(ronde), 0) AS n FROM games WHERE "
                             "tournament_id = ? AND fase = 'bracket'", (tid,)).fetchone()["n"])
        for paren in gepland:
            volgnr += 1
            for a, b, moment, loc in paren:
                db.execute("""
                    INSERT INTO games (team1_id, team2_id, tournament_id, fase, ronde,
                                       scheduled_at, location_id)
                    VALUES (?, ?, ?, 'bracket', ?, ?, ?)
                """, (a, b, tid, volgnr, moment.strftime(TIJDFORMAAT), loc))
    if doel != t["bracket_ronden"]:
        db.execute("UPDATE tournaments SET bracket_ronden = ? WHERE id = ?", (doel, tid))
    db.commit()

    naam = db.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()["name"]
    stuk = (f"iedereen speelt nu {doel} wedstrijden in plaats van "
            f"{t['bracket_ronden']}" if doel != t["bracket_ronden"]
            else f"iedereen speelt nog altijd {doel} wedstrijden")
    return True, (f"“{naam}” is teruggetrokken. Al hun wedstrijden — ook de al "
                  f"ingevulde forfaits — zijn geschrapt en er zijn "
                  f"{len(nieuw)} nieuwe affiches geloot, zodat {stuk}.")


def herloot(db, tid):
    """Zet een gegenereerd toernooi terug naar 'opzet' (alle wedstrijden weg)."""
    gespeeld = db.execute("SELECT COUNT(*) AS n FROM games WHERE tournament_id = ? "
                          "AND status = 'gespeeld'", (tid,)).fetchone()["n"]
    if gespeeld:
        return False, ("Er zijn al wedstrijden gespeeld. Verwijder eerst die resultaten "
                       "als je opnieuw wil loten.")
    db.execute("DELETE FROM games WHERE tournament_id = ?", (tid,))
    db.execute("UPDATE tournaments SET status = 'opzet' WHERE id = ?", (tid,))
    db.execute("UPDATE tournament_teams SET pot = NULL, seed = NULL "
               "WHERE tournament_id = ?", (tid,))
    db.commit()
    return True, "De loting is ongedaan gemaakt; je kan het toernooi opnieuw genereren."


# ------------------------------------------------------------------ stand --

def winstkwaliteit(db, tid, punten_van):
    """Hoe sterk waren de teams die je klopte, en die je tegenkwam?

    Twee klassieke maatstaven uit het schaken, hier berekend op de eindstand van
    de bracketfase:

    * **kwaliteit** (Sonneborn-Berger): de som van de punten van de teams die je
      versloeg. Klopte je de nummers 1 en 2, dan staat daar veel; klopte je twee
      hekkensluiters, dan weinig.
    * **programma** (Buchholz): de som van de punten van álle tegenstanders die
      je trof, gewonnen of verloren. Dat zegt hoe zwaar je lotingsprogramma was.

    Allebei worden ze pas ná de bracketfase berekend, uit de eindpunten. Het
    moment waarop je iemand versloeg speelt dus geen rol — anders zou je
    benadeeld worden omdat je de latere winnaar toevallig vroeg trof.
    """
    kwaliteit = {t: 0 for t in punten_van}
    programma = {t: 0 for t in punten_van}
    for g in db.execute("""
        SELECT team1_id, team2_id, winner_team_id FROM games
        WHERE tournament_id = ? AND fase = 'bracket' AND status = 'gespeeld'
    """, (tid,)):
        w = g["winner_team_id"]
        v = g["team2_id"] if w == g["team1_id"] else g["team1_id"]
        if w in kwaliteit and v in punten_van:
            kwaliteit[w] += punten_van[v]
        if w in programma and v in punten_van:
            programma[w] += punten_van[v]
        if v in programma and w in punten_van:
            programma[v] += punten_van[w]
    return kwaliteit, programma


def elo_na_bracketfase(db, tid):
    """Hoeveel Aura elk team wón of verlóór tijdens de bracketfase.

    Dit is een **toernooikracht**: iedereen begint op nul, ongeacht met welke
    rating hij binnenkwam. Enkel wat je vanavond deed telt mee. Een team dat
    even veel punten pakte maar tegen sterkere tegenstanders, staat hoger.

    Waarom niet gewoon de Aura zelf? Omdat die op een eerste toernooi voor
    iedereen 1000 is (en dus niets zegt), en later juist de geschiedenis zou
    laten meespelen: wie vorige maand goed speelde, zou vandaag een streepje
    voor krijgen bij een gelijke stand. Dat willen we niet.

    Knockoutwedstrijden tellen bewust niet mee: die komen later, en anders zou
    de bracketstand achteraf nog verschuiven.
    """
    eerste, laatste = {}, {}
    for r in db.execute("""
        SELECT rh.entity_id AS team_id, rh.elo_voor, rh.elo_na
        FROM rating_history rh
        JOIN games g ON g.id = rh.game_id
        WHERE rh.entity_type = 'team' AND rh.scope = 'permanent'
          AND g.tournament_id = ? AND g.fase = 'bracket'
        ORDER BY g.played_at, g.id
    """, (tid,)):
        eerste.setdefault(r["team_id"], r["elo_voor"])   # stand vóór de eerste
        laatste[r["team_id"]] = r["elo_na"]              # stand na de laatste
    return {t: laatste[t] - eerste[t] for t in laatste}


def _mini_punten(games, groep):
    """Punten die de teams uit `groep` onderling tegen elkaar pakten."""
    punten = {t: 0 for t in groep}
    gespeeld = {t: 0 for t in groep}
    for g in games:
        if g["team1_id"] in groep and g["team2_id"] in groep and g["status"] == "gespeeld":
            gespeeld[g["team1_id"]] += 1
            gespeeld[g["team2_id"]] += 1
            if g["winner_team_id"] in punten:
                punten[g["winner_team_id"]] += PUNTEN_WINST
    return punten, gespeeld


def _mini_verlies(games, groep):
    """Hoeveel onderlinge duels elk team uit `groep` verloor.

    Bij shootouts is dit essentieel: een team dat zijn shootout verlóór is
    uitgeschakeld, terwijl een team dat nog moet spelen enkel nog geen punten
    heeft. Zonder dit onderscheid lijken die twee even ver.
    """
    verlies = {t: 0 for t in groep}
    for g in games:
        if (g["team1_id"] in groep and g["team2_id"] in groep
                and g["status"] == "gespeeld"):
            for kant in (g["team1_id"], g["team2_id"]):
                if g["winner_team_id"] != kant:
                    verlies[kant] += 1
    return verlies


def stand(db, tid, hypothese=None):
    """De stand van de bracketfase, met alle tiebreakinformatie.

    Geeft een lijst dicts terug, gesorteerd van 1 naar laatst:
      positie, team_id, naam, pot, gespeeld, winst, verlies, punten,
      doorstoot (bool), gedeeld (bool: gelijk geëindigd, Aura besliste),
      onbeslist (bool: er is nog een shootout nodig)

    Met `hypothese` ({game_id: winnaar_id}) reken je een "wat als" door zonder
    iets weg te schrijven: die shootouts tellen dan mee alsof ze zo gespeeld
    zijn. Zo kunnen we scenario's uitrekenen terwijl er honderd mensen op de
    pagina zitten, zonder dat er ook maar één regel in de database verandert.
    """
    t = toernooi(db, tid)
    teams = {x["id"]: x for x in deelnemers(db, tid)}
    bracket = [g for g in games_van(db, tid, "bracket")]
    shootouts = [g for g in games_van(db, tid, "shootout")]
    if hypothese:
        shootouts = [dict(g, status="gespeeld", winner_team_id=hypothese[g["id"]])
                     if g["id"] in hypothese else g for g in shootouts]

    kracht_van = elo_na_bracketfase(db, tid)

    rijen = {}
    for tid_, team in teams.items():
        # start_elo = de permanente Aura op het moment van de loting (voor de potten).
        start = team["start_elo"] if team["start_elo"] is not None else team["elo"]
        rijen[tid_] = {"team_id": tid_, "naam": team["naam"], "pot": team["pot"],
                       "elo": start,
                       # Toernooikracht = de Aura die je vanavond won of verloor.
                       # Laatste tiebreak vóór de loting: wie zijn punten tegen
                       # sterkere tegenstanders pakte, staat hoger. Wie nog niet
                       # speelde staat op 0.
                       "kracht": kracht_van.get(tid_, 0.0),
                       "avatar": team["avatar"],
                       "spelers": team["spelers"],
                       "gespeeld": 0, "winst": 0, "verlies": 0, "punten": 0,
                       "shootouts": 0, "vorm": []}
    for g in bracket:
        if g["status"] != "gespeeld":
            continue
        for kant in (g["team1_id"], g["team2_id"]):
            if kant not in rijen:
                continue
            gewonnen = (g["winner_team_id"] == kant)
            rijen[kant]["gespeeld"] += 1
            rijen[kant]["winst"] += gewonnen
            rijen[kant]["verlies"] += (not gewonnen)
            rijen[kant]["punten"] += PUNTEN_WINST if gewonnen else 0
            rijen[kant]["vorm"].append("W" if gewonnen else "V")
    for g in shootouts:
        if g["status"] == "gespeeld" and g["winner_team_id"] in rijen:
            rijen[g["winner_team_id"]]["shootouts"] += 1

    # Kwaliteit van je overwinningen en zwaarte van je programma: die beslissen
    # vóór de Aura, omdat ze uit de eindstand komen en dus niet afhangen van het
    # moment waarop je iemand trof.
    punten_van = {team_id: r["punten"] for team_id, r in rijen.items()}
    kwaliteit, programma = winstkwaliteit(db, tid, punten_van)
    for team_id, r in rijen.items():
        r["kwaliteit"] = kwaliteit.get(team_id, 0)
        r["programma"] = programma.get(team_id, 0)

    # punten → onderling → shootouts → kwaliteit → programma → Aura → loting.
    lijst = list(rijen.values())
    lijst.sort(key=lambda r: (-r["punten"], -r["kwaliteit"], -r["programma"],
                              -r["kracht"], lot_sleutel(tid, r["team_id"])))

    geordend = []
    onbesliste_groepen = []
    i = 0
    while i < len(lijst):
        j = i
        while j + 1 < len(lijst) and lijst[j + 1]["punten"] == lijst[i]["punten"]:
            j += 1
        groep = lijst[i:j + 1]
        if len(groep) == 1:
            geordend.append(groep[0])
        else:
            geordend.extend(_orden_groep(groep, bracket, shootouts,
                                        onbesliste_groepen, tid))
        i = j + 1

    cut = t["ko_teams"]
    for plaats, r in enumerate(geordend, start=1):
        r["positie"] = plaats
        r["doorstoot"] = plaats <= cut
        r.setdefault("gedeeld", False)
        r["onbeslist"] = False

    # Zolang de bracketfase loopt, heeft het geen zin om over gelijke standen of
    # shootouts te spreken: dan staat de halve tabel nog gelijk. Geplande shootouts
    # tellen hier NIET mee — die zijn er net omdát er nog beslist moet worden.
    if not _bracketfase_gespeeld(db, tid):
        for r in geordend:
            r["gedeeld"] = False
        return geordend, []

    # Welke onbesliste groep beslist over een knockoutticket?
    ids_op_plaats = {r["team_id"]: r["positie"] for r in geordend}
    beslissend = []
    for groep in onbesliste_groepen:
        plaatsen = sorted(ids_op_plaats[t_] for t_ in groep)
        if not (plaatsen[0] <= cut < plaatsen[-1]):
            continue
        # Niet de hele groep speelt: enkel wie rond de streep staat en dus nog
        # écht iets te winnen of te verliezen heeft.
        strijd = strijdgroep(sorted(groep, key=lambda x: ids_op_plaats[x]),
                             ids_op_plaats, cut)
        if not strijd:
            continue
        beslissend.append(sorted(strijd))
        for r in geordend:
            if r["team_id"] in strijd:
                r["onbeslist"] = True
    return geordend, beslissend


def _volledig_onderling(games, ids):
    """Speelden álle teams uit deze groep onderling tegen elkaar?

    In een bracketfase loot je maar een deel van het veld, dus meestal niet. Het
    onderlinge resultaat mag pas beslissen als de mini-tabel compleet is: anders
    vergelijk je een team dat één onderling duel speelde met een team dat er drie
    speelde, en dat is geen eerlijke maatstaf.
    """
    nodig = {(a, b) for a in ids for b in ids if a < b}
    gespeeld = {tuple(sorted((g["team1_id"], g["team2_id"]))) for g in games
                if g["status"] == "gespeeld"
                and g["team1_id"] in ids and g["team2_id"] in ids}
    return nodig <= gespeeld


def strijdgroep(volgorde_in_groep, plaats_van, cut):
    """Wie speelt er écht voor een ticket?

    De hele groep staat gelijk op punten en ligt over de streep: van de m ploegen
    raken er k door. Zou iedereen een shootout spelen, dan win je met m/2 ploegen
    — en dat is zelden precies k. Het verschil komt terecht bij ploegen die er
    hoe dan ook in of uit liggen, en die spelen dus een partij waar ze zelf niets
    aan hebben. Dat is de kiem van een geregelde uitslag.

    Daarom laten we de bovenste k−j en de onderste m−k−j met rust — hun plaats
    verandert toch niet meer — en spelen de 2j ploegen rond de streep j partijen,
    telkens winnaar door en verliezer eruit. Met j = min(k, m−k) is dat het
    grootst mogelijke aantal echte beslissingswedstrijden, en geen enkele ploegen
    speelt nog voor spek en bonen.
    """
    m = len(volgorde_in_groep)
    k = sum(1 for t in volgorde_in_groep if plaats_van[t] <= cut)
    j = min(k, m - k)
    if j <= 0:
        return set()
    return set(volgorde_in_groep[k - j:k + j])


def _orden_groep(groep, bracket, shootouts, onbesliste_groepen, tid):
    """Orden teams die op punten gelijk staan: onderling resultaat eerst."""
    ids = {r["team_id"] for r in groep}
    if _volledig_onderling(bracket, ids):
        h2h, _ = _mini_punten(bracket, ids)
    else:
        h2h = {t: 0 for t in ids}      # onvolledige mini-tabel: telt niet mee
    so, _ = _mini_punten(shootouts, ids)
    so_verlies = _mini_verlies(shootouts, ids)

    # Hoeveel shootouts speelde elk team al? Wie er één gespeeld heeft, heeft
    # zijn kans gehad: er komt geen tweede ronde meer.
    gespeelde_so = {t: 0 for t in ids}
    for g in shootouts:
        if g["status"] != "gespeeld":
            continue
        for kant in (g["team1_id"], g["team2_id"]):
            if kant in gespeelde_so:
                gespeelde_so[kant] += 1

    def rekenwerk(r):
        # Wat het rekenwerk al kan onderscheiden, hoeft niemand te spelen:
        # eerst het onderlinge duel, dan de kwaliteit van je overwinningen,
        # dan je hele programma.
        return (-(h2h[r["team_id"]]), -r.get("kwaliteit", 0), -r.get("programma", 0))

    def sleutel(r):
        # De volgorde zonder shootouts: eerst het rekenwerk, dan de
        # toernooikracht en ten slotte de loting.
        return rekenwerk(r) + (-r.get("kracht", 0.0), lot_sleutel(tid, r["team_id"]))

    gesorteerd = sorted(groep, key=sleutel)

    # De shootout verdeelt enkel de plaatsen waar hij over gaat. Wie niet hoefde
    # te spelen, houdt de plaats die hij al had: hij is net vrijgesteld ómdat die
    # plaats vastlag, en dan hoort hij er ook niet onder te zakken omdat anderen
    # een beslissingswedstrijd wonnen. De deelnemers worden dus onderling
    # herschikt binnen de plaatsen die zij samen bezetten — winnaars bovenaan.
    speelde = [i for i, r in enumerate(gesorteerd)
               if so[r["team_id"]] or so_verlies[r["team_id"]]]
    if speelde:
        herschikt = sorted((gesorteerd[i] for i in speelde),
                           key=lambda r: (-(so[r["team_id"]]), so_verlies[r["team_id"]],
                                          sleutel(r)))
        for plaats, r in zip(speelde, herschikt):
            gesorteerd[plaats] = r

    # Er komt hoogstens ÉÉN shootoutronde per puntengroep. Is die gespeeld, dan
    # ligt de volgorde vast — ook voor wie niet meespeelde. Zo weet de zaal
    # meteen hoeveel shootouts er komen en kan er achteraf niets meer bijkomen.
    ronde_gehad = any(gespeelde_so[t] > 0 for t in ids)

    # Wie ook na het rekenwerk én de shootouts nog exact gelijk staat, blijft
    # onbeslist: enkel dán is een shootout nog zinvol.
    def zelfde(a, b):
        return (rekenwerk(a) == rekenwerk(b)
                and so[a["team_id"]] == so[b["team_id"]]
                and so_verlies[a["team_id"]] == so_verlies[b["team_id"]])

    k = 0
    while k < len(gesorteerd):
        m = k
        while m + 1 < len(gesorteerd) and zelfde(gesorteerd[m + 1], gesorteerd[k]):
            m += 1
        if m > k:
            deel = [r["team_id"] for r in gesorteerd[k:m + 1]]
            for r in gesorteerd[k:m + 1]:
                r["gedeeld"] = True
            if not ronde_gehad:
                onbesliste_groepen.append(deel)
        k = m + 1
    return gesorteerd


def tiebreak_groepen(db, tid):
    """Leg uit hoe elke gelijke stand beslist is (of nog beslist moet worden).

    Bedoeld om op het scherm te tonen: wie staat er gelijk, wat deden die teams
    onderling, en waarom staat de ene boven de andere. Geeft een lege lijst
    zolang de bracketfase niet uitgespeeld is — dan zegt een gelijke stand nog
    niets.
    """
    if not _bracketfase_gespeeld(db, tid):
        return []
    t = toernooi(db, tid)
    geordend, beslissend = stand(db, tid)
    bracket = games_van(db, tid, "bracket")
    shootouts = games_van(db, tid, "shootout")
    beslissende_ids = [set(x) for x in beslissend]
    cut = t["ko_teams"]
    inzet = shootout_inzet(db, tid)

    groepen = []
    i = 0
    while i < len(geordend):
        j = i
        while j + 1 < len(geordend) and geordend[j + 1]["punten"] == geordend[i]["punten"]:
            j += 1
        rijen = geordend[i:j + 1]
        i = j + 1
        if len(rijen) < 2:
            continue

        ids = {r["team_id"] for r in rijen}
        volledig = _volledig_onderling(bracket, ids)
        h2h, h2h_gespeeld = _mini_punten(bracket, ids)
        so, _ = _mini_punten(shootouts, ids)
        so_verlies = _mini_verlies(shootouts, ids)

        # De onderlinge duels zelf, zodat je kan tonen wié van wie won.
        duels = []
        for g in list(bracket) + list(shootouts):
            if g["team1_id"] in ids and g["team2_id"] in ids:
                staat_op_spel = inzet.get(g["id"], {})
                duels.append({"fase": g["fase"], "team1": g["team1_id"],
                              "team2": g["team2_id"], "status": g["status"],
                              "winnaar": g["winner_team_id"],
                              "moment": g["scheduled_at"], "game_id": g["id"],
                              "uit_bij_verlies": staat_op_spel.get("uit_bij_verlies", []),
                              "door_bij_winst": staat_op_spel.get("door_bij_winst", [])})
        duels.sort(key=lambda d: (d["fase"] != "bracket", d["moment"] or ""))

        # Vorm per team, net als in de gewone stand: één bolletje per shootout,
        # in de volgorde waarin ze gespeeld werden. Bij een kringetje zie je zo
        # meteen "W V" staan in plaats van een misleidend "gewonnen".
        so_vorm = {t: [] for t in ids}
        for d in duels:
            if d["fase"] != "shootout":
                continue
            for kant in (d["team1"], d["team2"]):
                if d["status"] != "gespeeld":
                    so_vorm[kant].append("?")
                else:
                    so_vorm[kant].append("W" if d["winnaar"] == kant else "V")

        raakt_ticket = any(ids & b for b in beslissende_ids)
        wacht = any(r.get("onbeslist") for r in rijen)

        # Doet de toernooikracht hier écht het werk? Enkel als twee ploegen die
        # naast elkaar staan op álles daarvóór gelijk zijn en toch uit elkaar
        # gehaald worden. Dan hoort dat cijfer ook op het scherm te staan.
        def gelijk_tot_kracht(a, b):
            return (h2h[a["team_id"]] == h2h[b["team_id"]]
                    and a.get("kwaliteit", 0) == b.get("kwaliteit", 0)
                    and a.get("programma", 0) == b.get("programma", 0)
                    and so[a["team_id"]] == so[b["team_id"]]
                    and so_verlies[a["team_id"]] == so_verlies[b["team_id"]])

        kracht_beslist = any(
            gelijk_tot_kracht(a, b) and round(a["kracht"], 6) != round(b["kracht"], 6)
            for a, b in zip(rijen, rijen[1:]))
        open_so = [d for d in duels if d["fase"] == "shootout" and d["status"] != "gespeeld"]
        if open_so and not wacht:
            # Er loopt nog een shootout in deze groep: de volgorde hieronder is
            # dus voorlopig. Dit als "beslist" tonen is misleidend.
            status, uitleg = "shootout_bezig", (
                f"Er {'staat' if len(open_so) == 1 else 'staan'} nog "
                f"{len(open_so)} shootout{'' if len(open_so) == 1 else 's'} op het "
                "programma in deze groep; de volgorde hieronder is dus nog voorlopig. "
                "Er komen er geen bij: ligt onderweg al vast wie doorstoot, dan "
                "vervallen de resterende, anders blijft het bij deze.")
        elif wacht:
            meespelers = sum(1 for r in rijen if r.get("onbeslist"))
            status, uitleg = "shootout", (
                "Deze teams staan gelijk op punten, op de kwaliteit van hun "
                "overwinningen én op hun programma — er valt niets meer te rekenen, "
                "en het gaat over een ticket voor de knockout. Wie ook daarna nog "
                f"sowieso boven of onder de streep eindigt, blijft buiten schot; de "
                f"{meespelers} ploegen rond de streep spelen om de overblijvende "
                "plaatsen. Elke partij is winnaar door, verliezer eruit, en ze gaan "
                "allemaal tegelijk door zodat niemand de andere uitslagen al kent.")
        elif volledig and len(set(h2h.values())) > 1:
            status, uitleg = "onderling", (
                "Alle teams speelden onderling tegen elkaar, dus dat onderlinge "
                "resultaat beslist.")
        elif any(d["fase"] == "shootout" and d["status"] == "gespeeld" for d in duels):
            # Er is hier een shootout gespeeld. Dat is het opvallendste wat er
            # gebeurd is, dus dat vermelden we eerst — ook al staat de rest van
            # de groep op kwaliteit of programma gerangschikt.
            status, uitleg = "shootout_klaar", (
                "Het rekenwerk bracht deze groep tot aan de streep, maar kon de "
                "ploegen op de streep zelf niet scheiden. Daar besliste een shootout "
                "wie het laatste ticket pakt. De shootout verdeelt enkel díe plaatsen: "
                "wie niet hoefde te spelen, houdt de plaats die hij al had.")
        elif len({r.get("kwaliteit", 0) for r in rijen}) > 1:
            status, uitleg = "kwaliteit", (
                "De kwaliteit van de overwinningen beslist: de som van de punten van "
                "de teams die je versloeg.")
        elif len({r.get("programma", 0) for r in rijen}) > 1:
            status, uitleg = "programma", (
                "Ze klopten even sterke teams, dus telt het hele programma mee: de "
                "punten van álle tegenstanders die je trof.")
        else:
            if len({round(r["kracht"], 6) for r in rijen}) > 1:
                grond = ("kwaliteit en programma zijn gelijk, dus beslist de "
                         "toernooikracht: de Aura die je vanavond won of verloor.")
            else:
                grond = ("deze teams zijn op geen enkele manier meer van elkaar te "
                         "onderscheiden: de onderlinge volgorde is geloot.")
            if not volledig:
                status, uitleg = "elo", (
                    "Deze teams speelden niet allemaal onderling tegen elkaar, dus het "
                    "onderlinge resultaat telt niet mee. Er staat geen ticket op het "
                    "spel, en " + grond)
            else:
                status, uitleg = "elo", (
                    "Onderling geeft dit geen uitsluitsel en er staat geen ticket op "
                    "het spel, dus " + grond)

        groepen.append({
            "punten": rijen[0]["punten"],
            "raakt_ticket": raakt_ticket,
            "status": status,
            "uitleg": uitleg,
            "kracht_beslist": kracht_beslist,
            "onderling_volledig": volledig,
            "cut": cut,
            "duels": duels,
            "teams": [{
                "team_id": r["team_id"], "naam": r["naam"], "positie": r["positie"],
                "doorstoot": r["doorstoot"], "elo": r["elo"],
                "kracht": r["kracht"],
                "h2h_punten": h2h[r["team_id"]], "h2h_gespeeld": h2h_gespeeld[r["team_id"]],
                "so_punten": so[r["team_id"]], "so_verlies": so_verlies[r["team_id"]],
                "kwaliteit": r.get("kwaliteit", 0), "programma": r.get("programma", 0),
                "so_vorm": so_vorm[r["team_id"]],
                "so_winst": sum(1 for x in so_vorm[r["team_id"]] if x == "W"),
                # Enkel voor wie zélf nog moet spelen ligt de plaats niet vast.
                # Wie buiten de shootout blijft, staat al waar hij eindigt.
                "voorlopig": any(d["fase"] == "shootout" and d["status"] != "gespeeld"
                                 and r["team_id"] in (d["team1"], d["team2"])
                                 for d in duels),
                "onbeslist": r.get("onbeslist", False),
            } for r in rijen],
        })
    return groepen


# ------------------------------------------------- shootouts & doorstroming --

def _bracketfase_gespeeld(db, tid):
    """Zijn alle gewone bracketwedstrijden gespeeld? (shootouts niet meegeteld)"""
    n = db.execute("SELECT COUNT(*) AS n FROM games WHERE tournament_id = ? "
                   "AND fase = 'bracket' AND status = 'gepland'", (tid,)).fetchone()["n"]
    return n == 0


def _bracket_klaar(db, tid):
    """Alles gespeeld, inclusief eventuele shootouts: klaar voor de knockout."""
    n = db.execute("SELECT COUNT(*) AS n FROM games WHERE tournament_id = ? "
                   "AND fase IN ('bracket', 'shootout') AND status = 'gepland'",
                   (tid,)).fetchone()["n"]
    return n == 0


def _shootout_paren(groep, rng):
    """Koppel de strijdende ploegen: winnaar door, verliezer eruit.

    `strijdgroep` levert altijd een even aantal ploegen die om precies evenveel
    tickets spelen, dus één willekeurige koppeling volstaat. Iedereen speelt
    exact één partij en die partij beslist over zijn eigen ticket — er is dus
    niemand die kan toegeven zonder er zelf onder te lijden. Omdat geen enkele
    ploeg twee keer speelt, kunnen alle partijen bovendien tegelijk op
    verschillende tafels: niemand kent de andere uitslagen al.
    """
    leden = sorted(groep)
    rng.shuffle(leden)
    if len(leden) % 2:                      # hoort niet te gebeuren
        leden = leden[:-1]
    return [(leden[i], leden[i + 1]) for i in range(0, len(leden), 2)]


def _maak_shootouts(db, t, groepen, rng=None):
    """Genereer beslissingswedstrijden voor de groepen die nog gelijk staan."""
    rng = rng or random.Random()
    tafel_ids = [l["id"] for l in locaties(db, t["id"])]
    start = _laatste_moment(db, t["id"], _starttijd(t)) + timedelta(
        minutes=t["slot_minuten"])
    paren = []
    for groep in groepen:
        paren.extend(_shootout_paren(groep, rng))
    if not paren:
        return 0

    # Verdeel over speelrondes: geen team twee keer tegelijk, tafels vol benut.
    rondes = verdeel_in_rondes(paren, rng, max_per_ronde=len(tafel_ids) or None)
    gepland, _ = _plan(rondes, tafel_ids, start, t["slot_minuten"])
    for groep in gepland:
        for a, b, moment, loc in groep:
            db.execute("""
                INSERT INTO games (team1_id, team2_id, tournament_id, fase,
                                   scheduled_at, location_id)
                VALUES (?, ?, ?, 'shootout', ?, ?)
            """, (a, b, t["id"], moment.strftime(TIJDFORMAAT), loc))
    db.commit()
    return len(paren)


def maak_knockout(db, t, geplaatst):
    """Bouw het volledige knockoutschema op (met lege vakken voor latere rondes)."""
    n = len(geplaatst)
    tafel_ids = [l["id"] for l in locaties(db, t["id"])]
    start = _laatste_moment(db, t["id"], _starttijd(t)) + timedelta(
        minutes=t["slot_minuten"])

    def nieuw(ronde, positie, volgende_id, slot):
        cur = db.execute("""
            INSERT INTO games (tournament_id, fase, ronde, positie, volgende_game_id,
                               volgende_slot, scheduled_at)
            VALUES (?, 'knockout', ?, ?, ?, ?, ?)
        """, (t["id"], ronde, positie, volgende_id, slot, start.strftime(TIJDFORMAAT)))
        return cur.lastrowid

    lagen = [[nieuw(2, 1, None, None)]]        # de finale
    grootte = 4
    while grootte <= n:
        vorige = lagen[-1]
        nieuwe = []
        for idx, ouder in enumerate(vorige):
            nieuwe.append(nieuw(grootte, 2 * idx + 1, ouder, 1))
            nieuwe.append(nieuw(grootte, 2 * idx + 2, ouder, 2))
        lagen.append(nieuwe)
        grootte *= 2

    eerste = lagen[-1]
    volgorde = seed_volgorde(n)
    for i, gid in enumerate(eerste):
        s1, s2 = volgorde[2 * i], volgorde[2 * i + 1]
        db.execute("UPDATE games SET team1_id = ?, team2_id = ? WHERE id = ?",
                   (geplaatst[s1 - 1], geplaatst[s2 - 1], gid))

    # Kalender: van de eerste knockoutronde naar de finale, tafels respecteren.
    rondes = [[(None, None)] * len(laag) for laag in reversed(lagen)]
    _tijden, _ = _plan(rondes, tafel_ids, start, t["slot_minuten"])
    for laag, tijden in zip(reversed(lagen), _tijden):
        for gid, (_a, _b, moment, loc) in zip(laag, tijden):
            db.execute("UPDATE games SET scheduled_at = ?, location_id = ? WHERE id = ?",
                       (moment.strftime(TIJDFORMAAT), loc, gid))

    for plaats, team_id in enumerate(geplaatst, start=1):
        db.execute("UPDATE tournament_teams SET seed = ? WHERE tournament_id = ? "
                   "AND team_id = ?", (plaats, t["id"], team_id))
    db.execute("UPDATE tournaments SET status = 'knockout' WHERE id = ?", (t["id"],))
    db.commit()


def _doorstoters(db, tid, cut, hypothese=None):
    """De verzameling teams die doorstoot — eventueel in een 'wat als'-scenario."""
    geordend, _ = stand(db, tid, hypothese)
    return frozenset(r["team_id"] for r in geordend[:cut])


def _scenarios(anderen, vast=None):
    """Alle mogelijke uitslagen van een reeks openstaande shootouts."""
    basis = dict(vast or {})
    for combinatie in range(2 ** len(anderen)):
        scenario = dict(basis)
        for i, ander in enumerate(anderen):
            scenario[ander["id"]] = (ander["team1_id"] if (combinatie >> i) & 1
                                     else ander["team2_id"])
        yield scenario


def _wis_zinloze_shootouts(db, tid):
    """Schrap geplande shootouts waarvan de uitslag niets meer beslist.

    Soms ligt na een paar shootouts al vast wie doorstoot, terwijl er nog een
    wedstrijd op het programma staat die enkel de volgorde ónder of bóven de
    streep verandert. Die laten spelen is tijdverlies: we halen ze weg.

    Voor elke geplande shootout rekenen we beide uitslagen door. Verandert de
    groep doorstoters niet, dan is de wedstrijd overbodig — tenzij het schema ze
    meteen opnieuw zou aanmaken, want dan blijven we in een kringetje draaien.
    """
    t = toernooi(db, tid)
    if not t or t["status"] != "bracket":
        return []
    cut = t["ko_teams"]
    open_games = _open_shootouts(db, tid)
    # Meer dan een handvol openstaande shootouts? Dan zijn er te veel combinaties
    # om door te rekenen; we schrappen dan niets en spelen ze gewoon allemaal.
    if len(open_games) > 7:
        return []

    geschrapt = []
    for g in open_games:
        anderen = [x for x in open_games if x["id"] != g["id"]
                   and x["id"] not in geschrapt]
        # Beslist deze wedstrijd iets, wélke uitslag de andere shootouts ook
        # krijgen? Enkel als het antwoord voor élke combinatie "nee" is, mag hij
        # weg. Anders zou hij later weer nodig kunnen zijn en opnieuw opduiken —
        # en dat is precies de verrassing die we willen vermijden.
        overbodig = True
        for scenario in _scenarios(anderen):
            uitkomsten = {_doorstoters(db, tid, cut, {**scenario, g["id"]: w})
                          for w in (g["team1_id"], g["team2_id"])}
            if len(uitkomsten) > 1:
                overbodig = False
                break
        if not overbodig:
            continue                               # beslist wel degelijk iets

        # Zou het schema hem meteen opnieuw aanmaken? Dan laten we hem staan.
        db.execute("DELETE FROM games WHERE id = ?", (g["id"],))
        db.commit()
        _, beslissend = stand(db, tid)
        if any({g["team1_id"], g["team2_id"]} & set(groep) for groep in beslissend):
            db.execute("""
                INSERT INTO games (id, team1_id, team2_id, tournament_id, fase,
                                   scheduled_at, location_id, status)
                VALUES (?, ?, ?, ?, 'shootout', ?, ?, 'gepland')
            """, (g["id"], g["team1_id"], g["team2_id"], tid, g["scheduled_at"],
                  g["location_id"]))
            db.commit()
            continue
        geschrapt.append(g["id"])
    return geschrapt


def _open_shootouts(db, tid):
    return db.execute("""
        SELECT * FROM games WHERE tournament_id = ? AND fase = 'shootout'
          AND status = 'gepland' AND team1_id IS NOT NULL AND team2_id IS NOT NULL
        ORDER BY scheduled_at, id
    """, (tid,)).fetchall()


_inzet_cache = {}                                  # {tid: (vingerafdruk, uitkomst)}


def _uitslagen_afdruk(db, tid):
    """Korte handtekening van alle uitslagen in dit toernooi."""
    h = hashlib.sha1()
    for r in db.execute("SELECT id, status, winner_team_id FROM games "
                        "WHERE tournament_id = ? ORDER BY id", (tid,)):
        h.update(f"{r[0]}:{r[1]}:{r[2]};".encode())
    return h.hexdigest()


def shootout_inzet(db, tid):
    """Wat staat er op het spel bij elke openstaande shootout?

    Staan er drie shootouts op het bord, dan lijkt het alsof je nog drie kansen
    hebt — terwijl één nederlaag je er soms meteen uit knikkert. Dat hoor je te
    weten vóór je speelt, niet erna.

    Per geplande shootout kijken we daarom, over álle uitslagen van de andere
    openstaande shootouts heen, of een ploeg bij verlies zeker uitgeschakeld is,
    of bij winst zeker door. We melden dat enkel als de andere uitslag het
    verschil maakt: "zeker door bij winst" zeggen tegen een ploeg die sowieso
    doorstoot, is geen nieuws.

    Geeft {game_id: {"uit_bij_verlies": [team_id, ...],
                     "door_bij_winst": [team_id, ...]}}.
    """
    t = toernooi(db, tid)
    if not t or t["status"] != "bracket":
        return {}
    # Dit blijft hetzelfde tot er een uitslag verandert, en de pagina wordt vaak
    # herladen. Eén keer rekenen volstaat dus.
    afdruk = _uitslagen_afdruk(db, tid)
    bewaard = _inzet_cache.get(tid)
    if bewaard and bewaard[0] == afdruk:
        return bewaard[1]

    cut = t["ko_teams"]
    open_games = _open_shootouts(db, tid)
    # Bij meer dan een handvol openstaande shootouts zijn er te veel scenario's
    # om door te rekenen. Dan zeggen we liever niets dan iets traags of iets fouts.
    if not open_games or len(open_games) > 6:
        _inzet_cache[tid] = (afdruk, {})
        return {}

    inzet = {}
    for g in open_games:
        anderen = [x for x in open_games if x["id"] != g["id"]]
        kanten = (g["team1_id"], g["team2_id"])
        # vlag[winnaar van deze partij][team] = [altijd door, altijd uit]
        vlag = {w: {k: [True, True] for k in kanten} for w in kanten}
        for scenario in _scenarios(anderen):
            for w in kanten:
                door = _doorstoters(db, tid, cut, {**scenario, g["id"]: w})
                for k in kanten:
                    vlag[w][k][0 if k not in door else 1] = False

        uit, zeker = [], []
        for k in kanten:
            tegen = kanten[1] if k == kanten[0] else kanten[0]
            bij_winst, bij_verlies = vlag[k][k], vlag[tegen][k]
            if bij_verlies[1] and not bij_winst[1]:
                uit.append(k)                                # verliezen = eruit
            if bij_winst[0] and not bij_verlies[0]:
                zeker.append(k)                              # winnen = zeker door
        inzet[g["id"]] = {"uit_bij_verlies": uit, "door_bij_winst": zeker}
    _inzet_cache[tid] = (afdruk, inzet)
    return inzet


def evalueer(db, tid):
    """Werk de toestand van het toernooi bij. Idempotent: mag na élk resultaat
    (en na elke herberekening) opnieuw gedraaid worden.

    Geeft een lijst met meldingen terug voor de gebruiker.
    """
    t = toernooi(db, tid)
    if not t or t["status"] == "opzet":
        return []
    meldingen = []

    # 1. Winnaars doorschuiven in het knockoutschema.
    if t["status"] in ("knockout", "afgelopen"):
        for g in db.execute("""
            SELECT * FROM games WHERE tournament_id = ? AND fase = 'knockout'
            ORDER BY ronde DESC, positie
        """, (tid,)).fetchall():
            if g["status"] == "gespeeld" and g["volgende_game_id"]:
                kolom = "team1_id" if g["volgende_slot"] == 1 else "team2_id"
                db.execute(f"UPDATE games SET {kolom} = ? WHERE id = ?",
                           (g["winner_team_id"], g["volgende_game_id"]))
        finale = db.execute("SELECT * FROM games WHERE tournament_id = ? AND fase = "
                            "'knockout' AND ronde = 2", (tid,)).fetchone()
        nieuwe_status = "afgelopen" if (finale and finale["status"] == "gespeeld") \
            else "knockout"
        if nieuwe_status != t["status"]:
            db.execute("UPDATE tournaments SET status = ? WHERE id = ?",
                       (nieuwe_status, tid))
            if nieuwe_status == "afgelopen":
                meldingen.append("De finale is gespeeld — het toernooi zit erop!")
        db.commit()
        return meldingen

    # 2. Bracketfase: klaar? Dan shootouts of het knockoutschema.
    if t["status"] != "bracket" or not _bracketfase_gespeeld(db, tid):
        return meldingen

    geordend, beslissend = stand(db, tid)
    shootouts = db.execute("SELECT * FROM games WHERE tournament_id = ? AND "
                           "fase = 'shootout'", (tid,)).fetchall()
    openstaand = [g for g in shootouts if g["status"] == "gepland"]

    if openstaand:
        # Nog niets gespeeld? Dan mogen de shootouts nog aangepast worden aan de
        # actuele stand (bv. na een gecorrigeerd resultaat).
        if len(openstaand) == len(shootouts):
            betrokken = {x for g in openstaand for x in (g["team1_id"], g["team2_id"])}
            nodig = {x for groep in beslissend for x in groep}
            if betrokken != nodig:
                db.execute("DELETE FROM games WHERE tournament_id = ? AND "
                           "fase = 'shootout' AND status = 'gepland'", (tid,))
                db.commit()
                if nodig:
                    aantal = _maak_shootouts(db, t, beslissend)
                    meldingen.append(
                        f"De shootouts zijn opnieuw bepaald: {aantal} wedstrijd(en) "
                        f"tussen {len(nodig)} teams die gelijk staan.")
                    return meldingen
                # Er valt niets meer te beslissen. Meteen doorgaan naar het
                # knockoutschema: anders blijft het toernooi in de bracketfase
                # hangen zonder dat er nog iets te spelen valt.
                meldingen.append("Er zijn geen shootouts meer nodig.")
                return evalueer(db, tid) + meldingen
        # Ligt het doorstoten intussen al vast? Dan hoeft de rest niet gespeeld.
        weg = _wis_zinloze_shootouts(db, tid)
        if weg:
            meldingen.append(
                f"{len(weg)} shootout(s) geschrapt: de uitslag daarvan verandert "
                "niets meer aan wie doorstoot.")
            return evalueer(db, tid) + meldingen
        return meldingen                      # wachten tot ze gespeeld zijn

    if beslissend:
        aantal = _maak_shootouts(db, t, beslissend)
        weg = _wis_zinloze_shootouts(db, tid)
        meldingen.append(
            f"{aantal - len(weg)} shootout(s) ingepland: er staan teams gelijk op "
            "punten en het onderlinge duel gaf geen uitsluitsel.")
        if _bracketfase_gespeeld(db, tid) and not db.execute(
                "SELECT 1 FROM games WHERE tournament_id = ? AND fase = 'shootout' "
                "AND status = 'gepland'", (tid,)).fetchone():
            return evalueer(db, tid) + meldingen
    else:
        geplaatst = [r["team_id"] for r in geordend[:t["ko_teams"]]]
        maak_knockout(db, t, geplaatst)
        meldingen.append(f"De bracketfase is afgelopen — de laatste "
                         f"{t['ko_teams']} teams zijn geloot in het knockoutschema.")
    return meldingen


def eerdere_wedstrijd(db, game):
    """Moet een van beide teams eerst nog een vroegere wedstrijd afwerken?

    De kalender zet de wedstrijden in speelrondes, en je hoort ze in die volgorde
    af te werken. Kan je de uitslag van je derde partij al invullen terwijl je
    eerste nog openstaat, dan klopt de stand tussendoor niet — en de Aura wordt
    chronologisch herrekend, dus de volgorde waarin uitslagen binnenkomen doet
    er echt toe.

    Geeft de eerste openstaande vroegere wedstrijd terug, of None.
    """
    if not game["tournament_id"] or game["fase"] != "bracket" or not game["scheduled_at"]:
        return None                       # league, shootout en knockout: niet van toepassing
    return db.execute("""
        SELECT * FROM games
        WHERE tournament_id = ? AND fase = 'bracket' AND status = 'gepland'
          AND scheduled_at < ?
          AND (team1_id IN (?, ?) OR team2_id IN (?, ?))
        ORDER BY scheduled_at, id LIMIT 1
    """, (game["tournament_id"], game["scheduled_at"],
          game["team1_id"], game["team2_id"],
          game["team1_id"], game["team2_id"])).fetchone()


def mag_wissen(db, game):
    """Mag dit toernooiresultaat gewist worden?

    Een uitslag mag niet verdwijnen zolang er al wedstrijden op voortbouwen die
    zélf gespeeld zijn — anders zou je een halve finale bewaren die met de oude
    winnaars gespeeld werd. Geeft None (mag) of de reden waarom niet.
    """
    tid = game["tournament_id"]
    if not tid:
        return None

    def tel(sql, *args):
        return db.execute(sql, args).fetchone()["n"]

    if game["fase"] in ("bracket", "shootout"):
        ko = tel("SELECT COUNT(*) AS n FROM games WHERE tournament_id = ? "
                 "AND fase = 'knockout' AND status = 'gespeeld'", tid)
        if ko:
            return (f"Er {'is' if ko == 1 else 'zijn'} al {ko} knockoutwedstrijd"
                    f"{'' if ko == 1 else 'en'} gespeeld die op deze uitslag "
                    "voortbouwen. Wis die eerst, te beginnen bij de laatste ronde.")
        if game["fase"] == "bracket":
            so = tel("SELECT COUNT(*) AS n FROM games WHERE tournament_id = ? "
                     "AND fase = 'shootout' AND status = 'gespeeld'", tid)
            if so:
                return (f"Er {'is' if so == 1 else 'zijn'} al {so} shootout"
                        f"{'' if so == 1 else 's'} gespeeld die op deze stand "
                        "voortbouwen. Wis die eerst.")
    elif game["fase"] == "knockout" and game["volgende_game_id"]:
        volgende = db.execute("SELECT * FROM games WHERE id = ?",
                              (game["volgende_game_id"],)).fetchone()
        if volgende and volgende["status"] == "gespeeld":
            return ("De volgende knockoutronde is al gespeeld. Wis eerst dat "
                    "resultaat en werk zo terug naar deze wedstrijd.")
    return None


def herstel_na_wissen(db, tid, fase, game=None):
    """Breek af wat na deze uitslag kwam, zodat er opnieuw beslist kan worden.

    Twee gevallen:

    * Een **knockoutuitslag** wissen: de winnaar die al doorgeschoven was naar de
      volgende ronde moet daar weer weg, anders blijft er een finalist staan die
      zijn halve finale niet meer gewonnen heeft.
    * Een **bracket- of shootoutuitslag** wissen: dan klopt het hele
      knockoutschema niet meer. Dat wordt weggegooid en het toernooi keert terug
      naar de bracketfase; `evalueer` bepaalt daarna opnieuw wie doorstoot.
    """
    if fase == "knockout":
        if game is not None and game["volgende_game_id"]:
            kolom = "team1_id" if game["volgende_slot"] == 1 else "team2_id"
            db.execute(f"UPDATE games SET {kolom} = NULL WHERE id = ?",
                       (game["volgende_game_id"],))
            # De volgende wedstrijd kan dan zelf niet meer gespeeld zijn.
            db.execute("UPDATE games SET status = 'gepland', winner_team_id = NULL, "
                       "played_at = NULL WHERE id = ? AND status = 'gespeeld'",
                       (game["volgende_game_id"],))
            db.commit()
        return
    if fase not in ("bracket", "shootout"):
        return
    db.execute("DELETE FROM games WHERE tournament_id = ? AND fase = 'knockout'", (tid,))
    if fase == "bracket":
        # Nog niet gespeelde shootouts horen bij de oude stand en verdwijnen mee.
        db.execute("DELETE FROM games WHERE tournament_id = ? AND fase = 'shootout' "
                   "AND status = 'gepland'", (tid,))
    db.execute("UPDATE tournament_teams SET seed = NULL WHERE tournament_id = ?", (tid,))
    db.execute("UPDATE tournaments SET status = 'bracket' WHERE id = ?", (tid,))
    db.commit()


def evalueer_alles(db):
    """Loop alle toernooien na (bv. na een herberekening).

    Ook afgelopen toernooien: wist de organisator de finale om een fout recht te
    zetten, dan moet het toernooi weer op 'knockout' springen. Enkel toernooien
    die nog in opbouw zijn hebben niets te evalueren.
    """
    meldingen = []
    for r in db.execute("SELECT id FROM tournaments WHERE status != 'opzet'").fetchall():
        meldingen.extend(evalueer(db, r["id"]))
    return meldingen


# ------------------------------------------------------------- weergave --

def kalender(db, tid):
    """Alle wedstrijden gegroepeerd per ronde, klaar om te tonen."""
    namen = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM teams")}
    loc_namen = {l["id"]: l["name"] for l in locaties(db, tid)}
    groepen = []

    for nummer in [r["ronde"] for r in db.execute(
            "SELECT DISTINCT ronde FROM games WHERE tournament_id = ? AND "
            "fase = 'bracket' ORDER BY ronde", (tid,))]:
        games = db.execute("SELECT * FROM games WHERE tournament_id = ? AND "
                           "fase = 'bracket' AND ronde = ? ORDER BY scheduled_at, id",
                           (tid, nummer)).fetchall()
        tijd = games[0]["scheduled_at"] if games else None
        groepen.append({"titel": f"Speelronde {nummer}", "tijd": tijd, "games": games})

    so = db.execute("SELECT * FROM games WHERE tournament_id = ? AND fase = 'shootout' "
                    "ORDER BY scheduled_at, id", (tid,)).fetchall()
    if so:
        groepen.append({"titel": "Shootouts",
                        "tijd": so[0]["scheduled_at"], "games": so})

    from elo import KO_LABEL
    for nummer in [r["ronde"] for r in db.execute(
            "SELECT DISTINCT ronde FROM games WHERE tournament_id = ? AND "
            "fase = 'knockout' ORDER BY ronde DESC", (tid,))]:
        games = db.execute("SELECT * FROM games WHERE tournament_id = ? AND "
                           "fase = 'knockout' AND ronde = ? ORDER BY positie",
                           (tid, nummer)).fetchall()
        groepen.append({"titel": KO_LABEL.get(nummer, f"Ronde van {nummer}"),
                        "tijd": games[0]["scheduled_at"] if games else None,
                        "games": games})
    return groepen, namen, loc_namen


def knockout_kolommen(db, tid):
    """Het knockoutschema als kolommen (eerste ronde → finale) voor de weergave."""
    from elo import KO_LABEL
    rondes = [r["ronde"] for r in db.execute(
        "SELECT DISTINCT ronde FROM games WHERE tournament_id = ? AND "
        "fase = 'knockout' ORDER BY ronde DESC", (tid,))]
    kolommen = []
    for nummer in rondes:
        games = db.execute("SELECT * FROM games WHERE tournament_id = ? AND "
                           "fase = 'knockout' AND ronde = ? ORDER BY positie",
                           (tid, nummer)).fetchall()
        kolommen.append({"ronde": nummer,
                         "titel": KO_LABEL.get(nummer, f"Ronde van {nummer}"),
                         "games": games})
    return kolommen


def loting_data(db, tid):
    """Potten en de daaruit gelote affiches — voor het lotingsscherm."""
    teams = {x["id"]: x for x in deelnemers(db, tid)}
    loc_namen = {l["id"]: l["name"] for l in locaties(db, tid)}
    potten = {}
    for team in teams.values():
        potten.setdefault(team["pot"] or 0, []).append(team)
    for lijst in potten.values():
        lijst.sort(key=lambda x: (-x["elo"], x["naam"]))
    potlijst = [{"nummer": p, "teams": potten[p]} for p in sorted(potten)]

    rondes = []
    for nummer in [r["ronde"] for r in db.execute(
            "SELECT DISTINCT ronde FROM games WHERE tournament_id = ? AND "
            "fase = 'bracket' ORDER BY ronde", (tid,))]:
        games = db.execute("SELECT * FROM games WHERE tournament_id = ? AND fase = "
                           "'bracket' AND ronde = ? ORDER BY scheduled_at, id",
                           (tid, nummer)).fetchall()
        affiches = []
        for g in games:
            a, b = teams.get(g["team1_id"]), teams.get(g["team2_id"])
            if not a or not b:
                continue
            affiches.append({
                "a": a["naam"], "a_pot": a["pot"], "a_elo": round(a["elo"]),
                "b": b["naam"], "b_pot": b["pot"], "b_elo": round(b["elo"]),
                "tijd": g["scheduled_at"],
                "locatie": loc_namen.get(g["location_id"]),
            })
        rondes.append({"nummer": nummer, "affiches": affiches,
                       "tijd": games[0]["scheduled_at"] if games else None})
    return potlijst, rondes
