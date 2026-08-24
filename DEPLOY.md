# Lebershuss Tonzent online zetten

Stap voor stap, van een lege server tot een werkende site op je eigen domein.
Reken op **een uurtje** de eerste keer.

Wat je nodig hebt:

- een **domeinnaam** (± €10–15 per jaar)
- een **VPS** — aanbevolen: Hetzner Cloud **CX22** (2 vCPU, 4 GB RAM, 40 GB
  schijf, ± €4,35/maand, datacenter Falkenstein of Nürnberg)
- je code in **git** (GitHub of GitLab, gerust een privérepo)

> **Waarom een VPS en niet gewone webhosting?** Deze site is een Python-app.
> Goedkope webhosting draait enkel PHP (WordPress en co.) en kan hier niets mee.
> Op een VPS ben je zelf beheerder: dat is één keer instellen, daarna niets meer.

---

## 1. Zet je code op GitHub

Je hebt lokaal al een git-repo. Maak op github.com een **lege privérepo** en
duw je code er naartoe (vervang `JOUWNAAM`):

```bash
git remote add origin git@github.com:JOUWNAAM/lebershuss.git
git branch -M main
git push -u origin main
```

Controleer dat `shuss.db`, `.secret_key`, `backups/` en `static/uploads/` er
**niet** in zitten — dat regelt `.gitignore` al:

```bash
git ls-files | grep -E "shuss.db|secret_key|uploads/|backups/"   # hoort leeg te zijn
```

## 2. Maak de server aan

1. Maak een account op [hetzner.com/cloud](https://www.hetzner.com/cloud).
2. **New project** → **Add server**.
3. Kies: locatie **Falkenstein** (of Nürnberg), image **Ubuntu 24.04**,
   type **CX22**.
4. Bij *SSH keys*: voeg je publieke sleutel toe. Heb je die nog niet, maak ze
   eerst op je eigen pc met `ssh-keygen -t ed25519` en plak de inhoud van
   `~/.ssh/id_ed25519.pub` (op Windows: `C:\Users\cedrl\.ssh\id_ed25519.pub`).
5. Maak de server aan. Je krijgt een **IP-adres**.

> ⚠️ Overal hieronder staat `203.0.113.45` als **voorbeeld**-IP (dat adres is
> officieel gereserveerd voor documentatie en bestaat dus niet echt). Vervang het
> telkens door het IP-adres van jouw server, anders krijg je
> *"Connection timed out"*.

Log in vanaf je eigen pc:

```bash
ssh root@203.0.113.45          # ← jouw eigen IP-adres
```

Scheelt tikwerk: zet dit in `~/.ssh/config` (op Windows
`C:\Users\<jij>\.ssh\config`), dan volstaat `ssh shuss`:

```
Host shuss
    HostName 203.0.113.45      # ← jouw eigen IP-adres
    User root
```

## 3. Laat je domein naar de server wijzen

Bij je domeinregistrar zet je twee DNS-records:

| Type | Naam | Waarde |
| --- | --- | --- |
| A | `@` | het IP-adres van je server |
| A | `www` | het IP-adres van je server |

Dit kan tot een uur duren. Testen:

```bash
ping lebershuss.be
```

Zolang dit niet het juiste IP toont, heeft stap 6 (https) geen zin.

## 4. Server klaarmaken

Alles hieronder gebeurt **op de server** (dus na `ssh root@…`).

```bash
apt update && apt upgrade -y
apt install -y python3-venv python3-pip nginx git ufw

# firewall: enkel ssh en web
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

# een aparte gebruiker: de site draait niet als root
adduser --disabled-password --gecos "" shuss
```

Word eerst de gebruiker `shuss`, en geef de server toegang tot je repo:

```bash
sudo -u shuss -H bash
ssh-keygen -t ed25519 -C "shuss-server" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

> **Waarom dit?** GitHub aanvaardt géén wachtwoorden meer voor git. Probeer je
> `git clone https://…` bij een privérepo, dan krijg je *"Password
> authentication is not supported"*. Deze sleutel lost dat op — en `git pull`
> vraagt later ook nooit meer iets.

Kopieer de regel die verschijnt (begint met `ssh-ed25519`) en plak ze op GitHub
bij **je repo → Settings → Deploy keys → Add deploy key**. Geef ze een naam als
*hetzner server* en laat *Allow write access* **uit** staan.

Haal daarna de code binnen (let op: `git@github.com:` met een dubbele punt, geen
`https://`) en installeer de pakketten:

```bash
git clone git@github.com:JOUWNAAM/lebershuss.git site
cd site
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
exit          # terug naar root
```

De eerste keer vraagt hij of je github.com vertrouwt: typ `yes`.

> **Liever geen gedoe met sleutels?** Maak de repo publiek; dan werkt
> `git clone https://github.com/JOUWNAAM/lebershuss.git site` zonder meer. Er
> staan geen geheimen in: de database, `.secret_key` en de profielfoto's zitten
> in `.gitignore` en wachtwoorden worden gehasht opgeslagen.

## 5. De site laten draaien als dienst

Zo start ze automatisch mee op en herstart ze vanzelf als er iets misgaat.

```bash
nano /etc/systemd/system/shuss.service
```

Plak dit:

```ini
[Unit]
Description=Lebershuss Tonzent
After=network.target

[Service]
User=shuss
WorkingDirectory=/home/shuss/site
Environment="TIJDZONE=Europe/Brussels"
Environment="ACHTER_PROXY=1"
ExecStart=/home/shuss/site/.venv/bin/python app.py --host 127.0.0.1 --poort 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

`HTTPS=1` zetten we er pas bij in stap 7, wanneer https echt werkt.

```bash
systemctl daemon-reload
systemctl enable --now shuss
systemctl status shuss          # hoort "active (running)" te tonen
```

## 6. nginx ervoor + https

```bash
nano /etc/nginx/sites-available/shuss
```

> ⚠️ **Vervang `lebershuss.be` hieronder door jouw eigen domein.** Vergeet je dat,
> dan lukt certbot straks niet: *"Could not automatically find a matching server
> block"*. Je krijgt dan wel een certificaat, maar het wordt niet geïnstalleerd.

```nginx
server {
    listen 80;
    server_name lebershuss.be www.lebershuss.be;

    client_max_body_size 4M;      # profielfoto's tot 3 MB

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Aanzetten en testen:

```bash
ln -s /etc/nginx/sites-available/shuss /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

Surf nu naar `http://lebershuss.be` — de site hoort te verschijnen.

Dan het gratis certificaat:

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d lebershuss.be -d www.lebershuss.be
```

Kies **redirect** wanneer hij vraagt of http naar https moet. Certbot vernieuwt
het certificaat voortaan zelf.

> Krijg je *"Could not automatically find a matching server block"*? Dan staat er
> een ander domein in `server_name`. Zet het juist, `nginx -t && systemctl reload
> nginx`, en installeer het certificaat dat je al hebt:
> `certbot install --cert-name jouwdomein.be` — er wordt dan geen nieuw
> certificaat aangevraagd.

## 7. https aanzetten in de app

Nu pas — anders kan je niet meer inloggen over http.

```bash
sed -i '/ACHTER_PROXY/a Environment="HTTPS=1"' /etc/systemd/system/shuss.service
systemctl daemon-reload && systemctl restart shuss
```

## 8. Eerste gebruik

1. Surf naar `https://lebershuss.be/registreren` en maak **jouw** account aan.
   Het eerste account is automatisch de eigenaar.
2. Ga naar *Organisatie → Instellingen* en vul bij **Adres van de site**
   `https://lebershuss.be` in.
3. Voeg de spelers toe, stel de teams samen, en zet *Accounts opeisen* open
   wanneer het toernooi begint.

Ben je een account kwijt of raak je niet meer binnen:

```bash
sudo -u shuss /home/shuss/site/.venv/bin/python /home/shuss/site/app.py --eigenaar "Jouw Naam"
```

---

## Later: een update uitrollen

Pushen naar GitHub alleen volstaat niet — de server moet de wijziging ophalen:

```bash
# op je eigen pc
git push

# op de server
sudo -u shuss -H bash -c 'cd ~/site && git pull && .venv/bin/pip install -r requirements.txt'
systemctl restart shuss
```

Controleer daarna even of alles nog draait:

```bash
systemctl status shuss --no-pager | head -3
```

`shuss.db`, `static/uploads/` en `.secret_key` staan in `.gitignore` en blijven
dus gewoon staan. Ook `pip install` mag je overslaan als je niets aan
`requirements.txt` veranderde — het kan geen kwaad om het toch te doen.

Wijzigde je `style.css` of `app.js`? Dan zorgt de service worker er zelf voor dat
bezoekers de nieuwe versie krijgen: de cachenaam wordt uit de inhoud van die
bestanden berekend en verandert dus automatisch mee.

## Back-ups meenemen van de server

De site maakt zelf kopieën in `~/site/backups/`, maar die staan op dezelfde
server. Haal er geregeld eentje naar je eigen pc — het makkelijkst via
*Organisatie → Instellingen → Back-up → Download een back-up*.

Vanaf je eigen pc kan het ook zo:

```bash
scp shuss@203.0.113.45:/home/shuss/site/backups/*.db .
```

Terugzetten na een ramp:

```bash
systemctl stop shuss
sudo -u shuss cp ~shuss/site/backups/shuss_20260828_193000_opstart.db ~shuss/site/shuss.db
systemctl start shuss
```

## Als er iets misgaat

```bash
systemctl status shuss          # draait de app?
journalctl -u shuss -n 50       # de laatste foutmeldingen van de app
nginx -t                        # klopt de nginx-configuratie?
tail -f /var/log/nginx/error.log
```

Negen van de tien keer is het één van deze drie: het DNS-record wijst nog niet
naar de server, de dienst is niet herstart na een wijziging, of `HTTPS=1` staat
aan terwijl het certificaat nog niet werkt.
