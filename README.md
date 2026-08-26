# 🥃 Leberschuss Tonzent

Webapplicatie voor de schusscompetitie van jeugdhuis Tonzent: leden bekijken de
klassementen, organisatoren plannen wedstrijden en vullen resultaten in.
Volledig in het Nederlands, draait lokaal, zonder externe diensten.

## Snel starten

```bash
pip install -r requirements.txt
python app.py
```

Surf daarna naar **http://localhost:5000**. Bij het starten toont de app ook het
adres op het netwerk (bv. `http://192.168.0.12:5000`) — dat is het adres voor de
gsm's van de spelers.

De site draait op **waitress**, een echte server die meerdere bezoekers tegelijk
bedient. De testserver van Flask deed er één per keer, wat merkbaar was zodra een
hele tafel tegelijk het klassement ververste.

Andere poort? `python app.py --poort 8080`.

De SQLite-database (`shuss.db`) wordt bij de eerste start automatisch aangemaakt.

**Wie is de organisator?** Er is geen algemeen adminwachtwoord: je bent
organisator omdat je eigen spelersaccount die rol heeft. Het **allereerste
account** dat zich registreert wordt automatisch de **eigenaar** en kan daarna
anderen organisator maken.

Werk je met een database van vóór deze versie (daar bestonden nog geen rollen),
of is de eigenaar zijn wachtwoord kwijt? Duid dan vanaf de terminal een eigenaar
aan — wie aan de server kan, kan sowieso al aan het databasebestand:

```bash
python app.py --eigenaar "Voornaam Achternaam"     # of: --eigenaar 4821
```

Demogegevens uitproberen? Draai eenmalig `python seed_demo.py` (en verwijder
`shuss.db` als je terug wil naar een lege competitie).

## Twee delen: league en toernooi

De site is opgesplitst in twee tabbladen die **dezelfde spelers en teams** delen.
Je kan dus vandaag een leaguewedstrijd spelen en morgen een toernooiwedstrijd, met
hetzelfde profiel en hetzelfde team.

> **Enkel met toernooien werken?** Dat kan: bij *Organisatie → Instellingen →
> Wat zien de spelers?* staat één vinkje, **“Leaguegedeelte tonen aan de
> spelers”**. Staat het uit, dan zien spelers alleen het toernooigedeelte — geen
> klassement, geen wedstrijden, geen statistieken, geen seizoenen — en is de
> toernooipagina meteen de startpagina. Organisatoren blijven alles zien (met een
> duidelijk *verborgen*-label), zodat je in alle rust kan klaarzetten. Later
> aanzetten is hetzelfde vinkje; er gaat niets verloren en er hoeft niets
> herstart te worden. Standaard staat de league **uit**.

| | League | Toernooi |
| --- | --- | --- |
| Structuur | seizoenen met speeldagen | bracketfase + knockout op één dag |
| Seizoens-ELO | ✅ telt mee | — |
| Permanente ELO | ✅ telt mee | ✅ telt mee (zwaarder naarmate je verder geraakt) |

**Permanente ELO** is je rating van altijd: elke wedstrijd die je ooit speelde telt
mee. Die staat op het klassement en op je profiel.

**Seizoens-ELO** begint elk seizoen opnieuw op 1000 voor iedereen. Enkel
leaguewedstrijden van dat seizoen tellen mee. Elk seizoen heeft zo zijn eigen
klassement, terwijl je permanente rating gewoon doorloopt.

## Het toernooiformaat

Naar het model van de nieuwe Champions League:

1. **Potten.** Alle deelnemende teams worden op permanente ELO verdeeld over
   (standaard) 4 potten. Pot 1 zijn de sterkste teams.
2. **Bracketfase.** Iedereen zit in één grote bracket. De organisator kiest hoeveel
   wedstrijden elk team speelt; de tegenstanders worden geloot uit de verschillende
   potten, niemand speelt twee keer tegen dezelfde ploeg. Winst = 3 punten.
   *Oneven aantal teams?* Geen probleem: dan rust er elke speelronde één team, maar
   speelt iedereen wél evenveel wedstrijden — de stand blijft dus perfect
   vergelijkbaar. Wiskundig moet het aantal wedstrijden per team dan wel even zijn
   (7 teams × 3 wedstrijden = 21 "halve" wedstrijden, dat gaat niet op); het
   organisatiepaneel zegt meteen welke waarden wél kunnen.
3. **Shootouts.** Staan teams gelijk op punten en beslist dat over een knockoutticket,
   dan telt het onderlinge resultaat — maar **enkel als alle betrokken teams ook echt
   tegen elkaar gespeeld hebben**. In een bracketfase loot je maar een deel van het
   veld, dus is die mini-tabel meestal onvolledig: dan zou je een team dat één onderling
   duel speelde vergelijken met een team dat er drie speelde. Is ze onvolledig, dan
   plant het toernooi automatisch beslissingswedstrijden in tussen álle teams die gelijk
   staan (winnen of verliezen, gelijk bestaat niet). Een shootout telt **niet** mee
   voor de ELO — hij beslist enkel wie doorstoot. De winnaars vormen de bovenste
   helft; blijft het spannend, dan volgt er vanzelf een kleinere extra ronde.
4. **Knockout.** De beste 2, 4, 8, 16, ... teams gaan door. Nummer 1 tegen de laagst
   geplaatste, zodat de nummers 1 en 2 elkaar pas in de finale kunnen treffen.

**Kalender met tafels.** Organisatoren maken per toernooi hun locaties aan
("Tafel 1", "Tuintafel", …). De kalender plant nooit meer wedstrijden tegelijk dan
er tafels zijn, en een team staat nooit op twee tafels tegelijk.

De planning kijkt daarbij naar het hele toernooi in één keer in plaats van ronde
per ronde: komt er een tafel vrij, dan schuift meteen een wedstrijd van verderop
in het programma naar voren. Zo staat geen enkele tafel stil en duurt de avond
exact `aantal wedstrijden ÷ aantal tafels` speelrondes — het theoretische minimum.
Voorbeeld: 7 teams, 4 wedstrijden per team en 2 tafels = 14 wedstrijden in
7 speelrondes (3u30 aan een half uur per wedstrijd) in plaats van 9.

**Lotingsscherm.** Elk geloot toernooi heeft een pagina voor het grote scherm: de
potten links, en daarnaast de affiches die je één voor één omdraait (spatiebalk of
knop). Ideaal om vlak voor de aftrap te tonen.

**ELO-gewicht per fase** (op de permanente ELO):

| Fase | Gewicht |
| --- | --- |
| Leaguewedstrijd | ×1 |
| Bracketfase | ×0,75 |
| Shootout | telt niet mee |
| Achtste finale | ×1,25 |
| Kwartfinale | ×1,5 |
| Halve finale | ×1,75 |
| Finale | ×2 |

Resultaten meld je precies zoals in de league: beide teams melden zelf de winnaar
via hun profiel, of een organisator vult ze in.

## Wat kan de site?

**Publiek** (geen login nodig)

- *Klassement*: ELO-ranglijst van alle spelers én van alle teams. Elk duo dat
  ooit samen gespeeld heeft, verschijnt automatisch als team.
- *Wedstrijden*: geplande affiches en gespeelde resultaten, per maand
  gegroepeerd, met de gewonnen/verloren ELO-punten per team.
- *Statistieken*: bovenaan het vaste **Recordboek** (langste en actuele
  winstreeks, meeste wedstrijden en overwinningen, beste winstpercentage,
  hoogste ELO ooit, grootste ELO-sprong en het onafscheidelijkste duo), daaronder
  klassementen (totaal en gemiddelde per wedstrijd) voor elke statistiek die de
  organisatoren aanmaken. Zolang er geen statistieken bestaan,
  toont deze pagina een nette placeholder.

**Organisatie** (voor accounts met de rol organisator of eigenaar)

Het organisatiepaneel is opgesplitst in aparte pagina's: *Overzicht* (wat vraagt je
aandacht), *Seizoenen*, *Wedstrijden*, *Toernooien*, *Spelers & teams*,
*Rangen & statistieken* en *Instellingen*.

- Rollen beheren bij *Spelers & teams → Organisatoren*:
  - **Elke organisator** kan iemand erbij nemen en zijn *eigen* rol teruggeven.
  - **Enkel de eigenaar** kan de rol van een ánder afnemen. Zo kan geen enkele
    organisator de rest buitenzetten.
  - De eigenaar is onaantastbaar: zijn account kan niet gewist, gedeactiveerd,
    hernoemd of van wachtwoord veranderd worden door een organisator. Hetzelfde
    geldt tussen organisatoren onderling — anders kon je een collega's account
    gewoon overnemen via een wachtwoordreset.
  - Stopt de eigenaar? Hij draagt het **eigenaarschap** over aan een andere
    organisator (bevestigen door `overdragen` te typen) en wordt zelf gewone
    organisator. Er is altijd exact één eigenaar.
- **Instellingen** is enkel voor de eigenaar. Daar staan de ingrepen die de hele
  database raken: het leaguegedeelte aan- of uitzetten, de K-factoren, back-ups,
  en het **leegmaken** of **terugzetten** van de database. Elk van die zware
  ingrepen vraagt een woord dat je zelf moet intypen, en maakt eerst automatisch
  een back-up in `backups/`:
  - *Alleen de geschiedenis wissen* (`wis geschiedenis`) — wedstrijden,
    toernooien, seizoenen en ratings weg; spelers en teams blijven en starten
    weer op 1000 ELO. Ideaal om een nieuw seizoen te beginnen.
  - *Alles wissen* (`wis alles`) — ook alle spelers, teams en profielfoto's. Je
    wordt uitgelogd; het eerste account dat zich daarna registreert is opnieuw de
    eigenaar. Rangen en statistiektypes blijven bestaan.
  - *Back-up terugzetten* (`terugzetten`) — kies een `.db`-bestand van je
    computer; dat vervangt de hele database. Het bestand wordt eerst
    gecontroleerd, dus een verkeerd bestand kan niets stukmaken.

- Toernooi aanmaken: kies de datum, het aantal wedstrijden per team, hoeveel teams
  doorstoten (2, 4, 8, 16, …), het aantal potten en de duur van een wedstrijd.
  Wijs daarna teams toe, voeg de tafels toe en klik op **Toernooi genereren**:
  potten, affiches, kalender en knockoutschema rollen er in één keer uit.
- Spelers toevoegen (iedereen start op 1000 ELO). Elke speler krijgt bij het
  registreren een **willekeurig spelersnummer van 4 cijfers** (bv. `#4821`) — aan
  het nummer kan je dus niet zien wie zich als eerste inschreef. Je vindt elkaar
  ermee terug bij het maken van een team, en je kan er ook mee inloggen.
- Een speler heeft twee namen: zijn **echte naam** (de accountnaam, waarmee hij
  inlogt — die ligt vast) en een **bijnaam** (hoe hij overal op de site
  verschijnt — die mag hij zelf altijd veranderen). Enkel een organisator kan een
  echte naam wijzigen, bij *Spelers & teams → Naam corrigeren*.

### Inschrijvingen van buiten de site

Schreven de spelers zich elders in (bv. via een formulier)? Dan hoeven ze niet
alles opnieuw te typen:

1. Maak bij *Spelers & teams → Speler toevoegen* elke speler aan met zijn echte
   naam. Die accounts hebben **nog geen wachtwoord** en kunnen dus nog niet
   inloggen — maar je kan er wel al mee werken.
2. Zet bij *Team samenstellen* de duo's klaar. Zo'n team is meteen actief; de
   uitnodiging heen en weer is niet nodig. Daarna kan je al loten en spelen.
3. Zet op de dag zelf *Accounts opeisen* open. Iedereen surft naar `/claimen`,
   tikt op zijn eigen naam en kiest op de volgende pagina een wachtwoord — daarna
   is hij ingelogd en kan hij uitslagen melden. Verkeerde naam aangetikt? Met
   *Toch iemand anders* sta je weer in de lijst.
4. Zet het venster weer dicht zodra iedereen binnen is.

**Op de beamer:** de knop *Toon op het grote scherm* geeft een paginagrote
**QR-code** naar de opeispagina, met het adres eronder voor wie niet kan scannen.
Het scherm ververst zichzelf en telt mee hoeveel spelers er nog moeten — handig
om te zien wanneer iedereen binnen is. De QR-code komt van het pakket `segno`
(zie `requirements.txt`); ontbreekt het, dan toont de pagina gewoon het adres.

Het adres in de QR-code wordt afgeleid uit het adres waarmee jij de site opent.
Werk je zelf via het IP-adres terwijl de spelers een domeinnaam gebruiken, vul
dat domein dan in bij *Instellingen → Adres van de site*. In alle andere gevallen
laat je dat veld gerust leeg.

**Let op:** zolang het venster openstaat, kan iedereen die de site bereikt een
van die accounts nemen. Zet het dus enkel open terwijl je erbij bent. Elke claim
komt met tijdstip en IP-adres in het **logboek** bij *Accounts opeisen*. Nam
iemand het verkeerde account, klik dan **Vrijgeven** bij die speler: het
wachtwoord wordt gewist en het account staat weer klaar.
- Wedstrijden plannen: kies vier spelers, teams worden automatisch aangemaakt
  of hergebruikt.
- Resultaten invullen (reserve-optie): normaal melden spelers zelf de uitslag;
  organisatoren zien de binnengekomen meldingen bij elke openstaande wedstrijd
  en kunnen zelf de winnaar invullen, bv. om een conflict te beslechten.
  Gelijkspel bestaat niet. Na elk resultaat worden alle ELO's bijgewerkt.
- Statistieken beheren: maak bv. "saves" aan en het veld verschijnt vanaf dan
  op elk resultaatformulier + krijgt een eigen bord op de statistiekenpagina.
  Statistieken hebben nooit invloed op ELO.
- Resultaten corrigeren: verwijder een fout resultaat; alle ratings worden dan
  automatisch chronologisch herberekend.
- Spelers en teams **definitief verwijderen** (bij *Spelers & teams*, achter
  “Definitief verwijderen…”). Je moet het woord `verwijder` intypen, zodat het
  nooit per ongeluk gebeurt.
  - Een **team** verdwijnt samen met al zijn wedstrijden; daarna wordt alles
    herberekend alsof die nooit gespeeld zijn — wie er ELO aan verloor, krijgt
    die exact terug. Speelt het team nog mee in een lopend toernooi, dan wordt
    het geweigerd (anders klopt het schema niet meer).
  - Een **speler** kan pas weg als hij in geen enkel team meer zit; de app zegt
    welke teams dat zijn. Wil je iemand enkel uit het klassement halen, gebruik
    dan *Deactiveren*: dan blijft de historiek bestaan.
- Per speler zie je hoeveel wedstrijden hij/zij deze maand al speelde
  (informatief — jullie bepalen zelf de maandlimiet).
- K-factoren instelbaar via *Instellingen*.

## Hoe werkt de ELO-berekening?

Elke wedstrijd is 2 tegen 2. Spelers én teams hebben elk een eigen rating.

- **Zijde-rating** van een kant = 50% team-ELO + 50% gemiddelde van de twee
  speler-ELO's.
- **Team-update**: klassieke ELO-formule op basis van de zijde-ratings van
  beide kanten (K-factor teams, standaard 32).
- **Speler-update**: de effectieve rating van een speler = 50% eigen ELO +
  25% ELO van de teammaat + 25% team-ELO. Die wordt afgezet tegen de
  zijde-rating van de tegenstanders (K-factor spelers, standaard 32).
- Een nieuw team start op het gemiddelde van de ELO's van zijn twee spelers op
  het moment van zijn eerste wedstrijd.

Zo weegt alles mee: je eigen niveau, je maat, je team en de sterkte van de
tegenstand. Winnen tegen een topduo levert veel punten op; verliezen van de
rode lantaarn kost er veel.

**Gewicht per fase.** Een leaguewedstrijd telt voor 1, de bracketfase van een
toernooi voor 0,75, en in de knockout loopt het op: achtste ×1,25, kwart ×1,5,
halve ×1,75, finale ×2. Een shootout telt helemaal niet mee.

**In de knockout betaalt de verliezer maar de helft.** Zonder die regel kostte
ver geraken je ELO: je kwartfinale verliezen kostte 24 punten terwijl je je
helemaal niet plaatsen niets kostte, en een late nederlaag woog zwaarder dan de
overwinning die je er bracht. Nu levert elke ronde die je overleeft ook echt
iets op:

| verloop | ELO |
| --- | --- |
| net niet gekwalificeerd | 0 |
| uit in de kwartfinale | −12 |
| kwart gewonnen, uit in de halve | +9 |
| tot de finale, daar verloren | +32 |
| toernooi gewonnen | +78 |

De keerzijde: in de knockout is de ELO geen zuiver nulsomspel meer — er komt per
toernooi wat ELO bij in omloop (± 90 punten, verspreid over alle deelnemers).
In de league en de bracketfase blijft het wél een nulsom: wat de ene wint,
verliest de andere.

## Bestanden

| Bestand | Rol |
| --- | --- |
| `app.py` | Flask-app: routes, leaderboards, adminpaneel |
| `elo.py` | ELO-berekening + gewicht per fase |
| `tournament.py` | Toernooimotor: potten, loting, kalender, stand, knockout |
| `database.py` | SQLite-schema, initialisatie en migratie |
| `test_toernooi.py` | Simulatie die een volledig toernooi uitspeelt en controleert |
| `*.html` | Jinja2-templates (frontend) |
| `style.css`, `app.js` | Vormgeving en interactie |
| `seed_demo.py` | Optionele demogegevens |
| `wsgi.py` | Toegangspunt voor een webserver (gunicorn, Passenger, uWSGI) |
| `shuss.db` | De database — maak hier af en toe een back-up van! |
| `backups/` | Automatische en handmatige kopieën van de database |
| `requirements.txt` | Benodigde pakketten (flask, waitress, segno) |
| `static/manifest.webmanifest`, `static/sw.js`, `static/icon-*.png` | Maken de site installeerbaar als app (PWA) |

De app werkt met alle bestanden in één map. Wil je het netter, dan mag je de
`.html`-bestanden in een submap `templates/` zetten en `style.css` + `app.js`
in `static/` — de app detecteert dat automatisch.

## Online zetten

Op een echte server met een domeinnaam ben je niet meer afhankelijk van het wifi
van het jeugdhuis, kan iedereen thuis meekijken, en werkt de app-installatie op
de gsm écht (die vraagt https).

**Waar kan het draaien?** De site is een Python/Flask-app. Gewone webhosting is
vaak enkel PHP (WordPress en co.) — dat volstaat níét. Vraag je hoster of je een
**Python-applicatie via WSGI** mag draaien en of je **SSH** krijgt. Kan dat niet,
neem dan een kleine **VPS**: daar installeer je Python zelf en zet je nginx of
Apache ervoor voor https.

> 📘 **[DEPLOY.md](DEPLOY.md)** bevat een volledige handleiding stap voor stap:
> van een lege VPS tot een werkende site op je eigen domein, met https,
> automatisch herstarten en updates uitrollen.

**Zo zet je hem op:**

```bash
pip install -r requirements.txt
```

Wijs de webserver naar `wsgi.py` (zie de uitleg bovenaan dat bestand), of draai
hem eenvoudig met `python app.py --poort 8000` achter nginx.

**Zet deze drie omgevingsvariabelen:**

| Variabele | Waarde | Waarom |
| --- | --- | --- |
| `TIJDZONE` | `Europe/Brussels` | Servers staan meestal op UTC; anders staan álle uren twee uur verkeerd. Dit is de standaard, dus meestal hoef je niets te doen. |
| `HTTPS` | `1` | De sessiecookie mag dan enkel over https. Zet dit pas als https echt werkt. |
| `ACHTER_PROXY` | `1` | Enkel wanneer er een webserver (nginx, Apache, Passenger) vóór de app staat. Dan zie je het echte IP-adres van bezoekers in het claimlogboek. **Nooit aanzetten zonder zo'n webserver**: iedereen kan dan zelf een IP-adres meesturen. |

**Deze bestanden moeten blijven staan bij een update of herstart** — ze staan
daarom ook in `.gitignore`:

- `shuss.db` — de volledige competitie
- `static/uploads/` — de profielfoto's
- `.secret_key` — anders is iedereen bij de eerste herstart uitgelogd

**Back-ups.** Bij het opstarten maakt de site automatisch een kopie in `backups/`
(hoogstens één per uur, de 20 nieuwste blijven staan). Download er zelf ook
geregeld eentje via *Organisatie → Instellingen → Back-up*, zeker rond een
toernooi — een kopie op de server helpt niet als de server zelf wegvalt.
Terugzetten: hernoem het back-upbestand naar `shuss.db` en herstart.

**Na het online zetten:** vul bij *Instellingen → Adres van de site* je domein in
als je zelf via een ander adres werkt, zodat de QR-code naar het juiste adres
wijst.

## Op de beamer

Op de toernooipagina staat voor organisatoren een knop **📺 Beamermodus**. Die
opent dezelfde pagina met `?beamer=1` erachter en:

- **ververst zichzelf om de 20 seconden**, dus de stand loopt de hele avond mee
  zonder dat je iets moet doen (rechtsonder zie je aftellen);
- **blijft op hetzelfde tabblad** staan — kies *Stand* of *Knockout* en dat blijft
  zo na elke verversing, want het tabblad staat mee in het adres;
- **haalt alles weg wat afleidt** (menu, voettekst, je eigen wedstrijdbalk) en
  toont de tabellen groter, zodat ze achteraan de zaal leesbaar zijn.

Zet het venster op volledig scherm (F11) en laat het gerust openstaan. Staat het
tabblad op de achtergrond, dan ververst het niet — zo verspil je niets.

## Op je startscherm zetten

De site heeft een manifest, een service worker en app-iconen, dus je kan ze op je
gsm op het startscherm zetten:

- *iPhone (Safari)*: deelknop → **Zet op beginscherm**. Opent zonder browserbalk,
  met het Leberschuss-logo. Werkt gewoon op het netwerk van het jeugdhuis.
- *Android (Chrome)*: onderaan de site verschijnt vanzelf een balkje met een
  knop **Installeren**. Liever via het menu? ⋮ → **App installeren**.

Draait de site nog zonder https (bv. rechtstreeks op een laptop in het
jeugdhuis), dan tonen browsers die installatieknop niet — dat is een regel van
de browser, niet van deze site. Op een echt domein met certificaat werkt het
wel.

## Tips voor gebruik in het jeugdhuis

- Draai de app op één vaste laptop of mini-pc; andere toestellen op hetzelfde
  netwerk kunnen surfen naar `http://<ip-van-die-machine>:5000`.
- Back-up = het bestand `shuss.db` kopiëren. Meer is het niet.
- Dit is een interne clubtool; zet hem niet zomaar open op het internet.
