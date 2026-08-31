# -*- coding: utf-8 -*-
"""
ELO-motor voor Leberschuss Tonzent.

Op de site heet deze rating **Aura**. In de code houden we de technische naam
ELO aan — het is letterlijk het systeem van Arpad Elo — net als de kolomnamen
`players.elo` en `teams.elo`. Wie de tekst op het scherm wil aanpassen, zoekt
dus naar "Aura" in de templates, niet hier.

Een wedstrijd is altijd 2 tegen 2. Elke speler heeft een eigen ELO-rating,
en elk team (elk duo dat ooit samen gespeeld heeft) heeft ook een ELO-rating.

De rating-update van een speler is gebaseerd op:
  - zijn eigen ELO            (gewicht 0.50)
  - de ELO van zijn teammaat  (gewicht 0.25)
  - de ELO van zijn team      (gewicht 0.25)
  - de ELO's van de tegenstanders én het tegenstander-team (via de "zijde-rating")

De rating-update van een team is gebaseerd op de samengestelde zijde-rating
van beide kanten (50% team-ELO, 50% gemiddelde speler-ELO).
"""

START_ELO = 1000.0

# Hoe zwaar telt een wedstrijd mee voor de PERMANENTE ELO?
# Leaguewedstrijden zijn de norm (1.0). Een toernooiwedstrijd weegt in de
# bracketfase iets lichter, maar hoe verder een team geraakt, hoe zwaarder.
FASE_FACTOR = {
    "liga": 1.00,
    "bracket": 0.75,
    # Een shootout is geen echte wedstrijd maar een beslissingsprocedure: hij
    # bepaalt enkel wie doorstoot en laat de ratings volledig ongemoeid.
    "shootout": 0.00,
}

# Knockoutrondes, per aantal teams dat nog meedoet.
KO_FACTOR = {
    2: 2.00,    # finale
    4: 1.75,    # halve finale
    8: 1.50,    # kwartfinale
    16: 1.25,   # achtste finale
}

FASE_LABEL = {
    "liga": "Leaguewedstrijd",
    "bracket": "Bracketfase",
    "shootout": "Shootout",
    "knockout": "Knockout",
}

KO_LABEL = {2: "Finale", 4: "Halve finale", 8: "Kwartfinale",
            16: "Achtste finale", 32: "Zestiende finale"}


# In de knockout weegt een nederlaag maar half zo zwaar als een overwinning.
#
# Waarom? Met een oplopend gewicht (kwart 1,5 → finale 2,0) betaalde je vroeger
# meer voor een late nederlaag dan je verdiende met de winst die je er bracht:
# de kwartfinale verliezen kostte 24 punten, terwijl je je helemaal niet
# plaatsen niets kostte. Wie verder geraakte, kon daar dus ELO op verliezen.
# Door de nederlaag te halveren wordt elke ronde die je overleeft ook echt
# beloond. De keerzijde: er komt een beetje ELO bij in omloop.
KO_VERLIES = 0.50


def fase_factor(fase: str, ronde=None, verloren: bool = False) -> float:
    """Gewicht van een wedstrijd voor de permanente ELO.

    `verloren=True` geeft het gewicht voor de verliezende kant; in de knockout
    is dat de helft. In alle andere fases wegen winst en verlies even zwaar,
    zodat de ELO daar een zuiver nulsomspel blijft.
    """
    if fase == "knockout":
        try:
            n = int(ronde)
        except (TypeError, ValueError):
            n = None
        f = KO_FACTOR.get(n, 1.25)
        return f * KO_VERLIES if verloren else f
    return FASE_FACTOR.get(fase, 1.0)


def fase_omschrijving(fase: str, ronde=None) -> str:
    """Leesbaar label, bv. 'Kwartfinale' of 'Bracketronde 3'."""
    if fase == "knockout":
        try:
            return KO_LABEL.get(int(ronde), f"Ronde van {ronde}")
        except (TypeError, ValueError):
            return "Knockout"
    if fase == "bracket":
        return f"Speelronde {ronde}" if ronde else "Bracketfase"
    return FASE_LABEL.get(fase, "Wedstrijd")


def verwacht(r_eigen: float, r_tegen: float) -> float:
    """Verwachte score (winstkans) volgens de klassieke ELO-formule."""
    return 1.0 / (1.0 + 10 ** ((r_tegen - r_eigen) / 400.0))


def zijde_rating(team_elo: float, speler_elos: list) -> float:
    """Samengestelde sterkte van één kant: 50% team-ELO, 50% gemiddelde spelers."""
    return 0.5 * team_elo + 0.5 * (sum(speler_elos) / len(speler_elos))


def effectieve_rating(eigen: float, maat: float, team_elo: float) -> float:
    """Effectieve rating van een individuele speler binnen zijn team."""
    return 0.5 * eigen + 0.25 * maat + 0.25 * team_elo


def proces_wedstrijd(spelers1: dict, spelers2: dict,
                     team1_elo: float, team2_elo: float,
                     winnaar: int, k_speler: float = 32.0,
                     k_team: float = 32.0, verlies_deel: float = 1.0):
    """
    Verwerk één wedstrijd.

    spelers1 / spelers2 : dict {speler_id: elo} met telkens exact 2 spelers
    winnaar             : 1 als team 1 wint, 2 als team 2 wint (gelijkspel bestaat niet)
    verlies_deel        : hoeveel van de puntenafname de verliezer betaalt.
                          1,0 = een zuiver nulsomspel (wat de winnaar wint,
                          verliest de tegenstander). In de knockout staat dit op
                          0,5, zodat ver geraken nooit ELO kost.

    Geeft terug: (nieuwe_speler_elos: dict {id: elo}, nieuw_team1_elo, nieuw_team2_elo)
    """
    def weeg(delta):
        """De verliezer (negatieve delta) betaalt eventueel maar een deel."""
        return delta * verlies_deel if delta < 0 else delta

    if winnaar not in (1, 2):
        raise ValueError("winnaar moet 1 of 2 zijn")
    if len(spelers1) != 2 or len(spelers2) != 2:
        raise ValueError("elk team moet exact 2 spelers hebben")

    zijde1 = zijde_rating(team1_elo, list(spelers1.values()))
    zijde2 = zijde_rating(team2_elo, list(spelers2.values()))

    s1 = 1.0 if winnaar == 1 else 0.0
    s2 = 1.0 - s1

    nieuw_team1 = team1_elo + weeg(k_team * (s1 - verwacht(zijde1, zijde2)))
    nieuw_team2 = team2_elo + weeg(k_team * (s2 - verwacht(zijde2, zijde1)))

    nieuwe_spelers = {}
    for eigen_dict, andere_zijde, score, eigen_team_elo in (
        (spelers1, zijde2, s1, team1_elo),
        (spelers2, zijde1, s2, team2_elo),
    ):
        for pid in eigen_dict:
            maat_elo = next(v for k, v in eigen_dict.items() if k != pid)
            eff = effectieve_rating(eigen_dict[pid], maat_elo, eigen_team_elo)
            nieuwe_spelers[pid] = eigen_dict[pid] + weeg(
                k_speler * (score - verwacht(eff, andere_zijde)))

    return nieuwe_spelers, nieuw_team1, nieuw_team2
