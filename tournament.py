# -*- coding: utf-8 -*-
"""
Toernooimotor van Lebershuss Tonzent.

Formaat (naar het model van de nieuwe Champions League):

1. **Bracketfase** — álle teams zitten in één grote bracket. De organisator kiest
   hoeveel wedstrijden elk team speelt. De tegenstanders worden geloot uit de
   potten (pot 1 = sterkste teams volgens permanente ELO), zodat elk team een
   evenwichtig programma krijgt en niemand twee keer dezelfde tegenstander loot.
2. **Shootouts** — staan er na de bracketfase teams gelijk op punten én beslist
   dat over een knockoutticket, dan genereert het toernooi automatisch extra
   beslissingswedstrijden. Het onderlinge resultaat telt enkel als álle betrokken
   teams onderling gespeeld hebben (anders vergelijk je ongelijke steekproeven);
   in alle andere gevallen volgt een shootout — winnen of verliezen, gelijk bestaat
   niet. Een shootout beslist enkel wie doorstoot en telt niet mee voor de ELO.
3. **Knockout** — de beste 2, 4, 8, 16, ... teams gaan door. Nummer 1 speelt
   tegen de laagste geplaatste, enzovoort, zodat de nummers 1 en 2 elkaar pas in
   de finale kunnen tegenkomen.

De kalender houdt rekening met de beschikbare locaties ("tafel 1", "tuintafel", ...):
per ronde spelen er nooit meer wedstrijden tegelijk dan er tafels zijn, en een
team staat nooit op twee tafels tegelijk.
"""

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


def deelnemers(db, tid):
    """Teams van dit toernooi, met naam, permanente ELO en de twee spelers."""
    rijen = db.execute("""
        SELECT tt.team_id AS id, tt.pot, tt.seed, tt.start_elo,
               t.name AS naam, t.elo, t.avatar,
               t.player1_id, t.player2_id
        FROM tournament_teams tt
        JOIN teams t ON t.id = tt.team_id
        WHERE tt.tournament_id = ?
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


def potten_verdelen(teams, aantal_potten):
    """Verdeel de teams (gesorteerd op permanente ELO) over de potten.

    Geeft {team_id: potnummer} terug; pot 1 bevat de sterkste teams.
    """
    n = len(teams)
    aantal_potten = max(1, min(aantal_potten, n))
    gesorteerd = sorted(teams, key=lambda t: (-t["elo"], t["naam"]))
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


def verdeel_in_rondes(paren, rng=None, max_per_ronde=None, pogingen=150):
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
                    return gevuld
        if beste:
            return beste
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
    pot_van = potten_verdelen(teams, t["potten"])
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
                                    max_per_ronde=len(tafel_ids) or None)
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


def stand(db, tid):
    """De stand van de bracketfase, met alle tiebreakinformatie.

    Geeft een lijst dicts terug, gesorteerd van 1 naar laatst:
      positie, team_id, naam, pot, gespeeld, winst, verlies, punten,
      doorstoot (bool), gedeeld (bool: gelijk geëindigd, ELO besliste),
      onbeslist (bool: er is nog een shootout nodig)
    """
    t = toernooi(db, tid)
    teams = {x["id"]: x for x in deelnemers(db, tid)}
    bracket = [g for g in games_van(db, tid, "bracket")]
    shootouts = [g for g in games_van(db, tid, "shootout")]

    rijen = {}
    for tid_, team in teams.items():
        # start_elo = de permanente ELO op het moment van de loting. Zo blijft de
        # stand stabiel, ook als de ELO tijdens het toernooi verandert.
        rijen[tid_] = {"team_id": tid_, "naam": team["naam"], "pot": team["pot"],
                       "elo": team["start_elo"] if team["start_elo"] is not None
                              else team["elo"],
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

    # Sorteren: punten → onderling (bracket) → onderling (shootouts) → ELO → naam.
    lijst = list(rijen.values())
    lijst.sort(key=lambda r: (-r["punten"], -r["elo"], r["naam"]))

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
            geordend.extend(_orden_groep(groep, bracket, shootouts, onbesliste_groepen))
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
        if plaatsen[0] <= cut < plaatsen[-1]:
            beslissend.append(groep)
            for r in geordend:
                if r["team_id"] in groep:
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


def _orden_groep(groep, bracket, shootouts, onbesliste_groepen):
    """Orden teams die op punten gelijk staan: onderling resultaat eerst."""
    ids = {r["team_id"] for r in groep}
    if _volledig_onderling(bracket, ids):
        h2h, _ = _mini_punten(bracket, ids)
    else:
        h2h = {t: 0 for t in ids}      # onvolledige mini-tabel: telt niet mee
    so, _ = _mini_punten(shootouts, ids)
    so_verlies = _mini_verlies(shootouts, ids)

    def sleutel(r):
        # Wie zijn shootout won staat bovenaan, wie nog moet spelen ertussen,
        # wie verloor onderaan. Zo is een verliezer meteen uitgeklaard.
        return (-(h2h[r["team_id"]]), -(so[r["team_id"]]), so_verlies[r["team_id"]],
                -r["elo"], r["naam"])

    gesorteerd = sorted(groep, key=sleutel)

    # Teams met exact dezelfde tiebreakwaarden blijven onbeslist.
    k = 0
    while k < len(gesorteerd):
        m = k
        while (m + 1 < len(gesorteerd)
               and h2h[gesorteerd[m + 1]["team_id"]] == h2h[gesorteerd[k]["team_id"]]
               and so[gesorteerd[m + 1]["team_id"]] == so[gesorteerd[k]["team_id"]]
               and so_verlies[gesorteerd[m + 1]["team_id"]]
                   == so_verlies[gesorteerd[k]["team_id"]]):
            m += 1
        if m > k:
            deel = {r["team_id"] for r in gesorteerd[k:m + 1]}
            onbesliste_groepen.append(deel)
            for r in gesorteerd[k:m + 1]:
                r["gedeeld"] = True
        k = m + 1
    return gesorteerd


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
    """Zo weinig mogelijk beslissingswedstrijden om een gelijke groep te splitsen.

    De teams worden willekeurig gekoppeld: de winnaars vormen de bovenste helft,
    de verliezers de onderste. Beslist dat nog niet over het laatste ticket, dan
    volgt er vanzelf een nieuwe (kleinere) ronde. Bij een oneven groep spelen
    drie teams een onderling driehoekje.
    """
    leden = sorted(groep)
    rng.shuffle(leden)
    paren = []
    if len(leden) % 2:
        a, b, c = leden[:3]
        paren += [(a, b), (b, c), (a, c)]
        leden = leden[3:]
    for i in range(0, len(leden), 2):
        paren.append((leden[i], leden[i + 1]))
    return paren


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
                else:
                    meldingen.append("Er zijn geen shootouts meer nodig.")
                return meldingen
        return meldingen                      # wachten tot ze gespeeld zijn

    if beslissend:
        aantal = _maak_shootouts(db, t, beslissend)
        meldingen.append(
            f"{aantal} shootout(s) ingepland: er staan teams gelijk op punten "
            "en het onderlinge duel gaf geen uitsluitsel.")
    else:
        geplaatst = [r["team_id"] for r in geordend[:t["ko_teams"]]]
        maak_knockout(db, t, geplaatst)
        meldingen.append(f"De bracketfase is afgelopen — de laatste "
                         f"{t['ko_teams']} teams zijn geloot in het knockoutschema.")
    return meldingen


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


def herstel_na_wissen(db, tid, fase):
    """Breek af wat na deze fase kwam, zodat de stand opnieuw mag beslissen.

    Wordt een bracket- of shootoutuitslag gewist, dan klopt het knockoutschema
    niet meer: dat wordt weggegooid en het toernooi keert terug naar de
    bracketfase. `evalueer` bepaalt daarna opnieuw wie doorstoot.
    """
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
    """Loop alle lopende toernooien na (bv. na een herberekening)."""
    meldingen = []
    for r in db.execute("SELECT id FROM tournaments WHERE status IN "
                        "('bracket', 'knockout')").fetchall():
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
