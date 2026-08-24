/* Service worker van Lebershuss Tonzent.
 *
 * De site draait op de laptop in het jeugdhuis en de gegevens veranderen de hele
 * avond door. Daarom: pagina's ALTIJD eerst van het netwerk halen, zodat je nooit
 * een oude stand ziet. Enkel als het netwerk wegvalt, tonen we wat we hebben.
 * Vaste bestanden (stijl, script, iconen) komen wel eerst uit de cache.
 */
/* __VERSIE__ wordt door de server ingevuld met een code die uit de inhoud van
 * style.css, app.js en de iconen berekend is. Verandert er één van, dan krijgt
 * de cache een nieuwe naam en wordt de oude opgeruimd — bezoekers zien je
 * aanpassingen dus meteen na een update, zonder iets te moeten wissen. */
const VERSIE = "lebershuss-__VERSIE__";
const VASTE_BESTANDEN = [
  "/static/style.css",
  "/static/app.js",
  "/static/logo.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(VERSIE).then((c) => c.addAll(VASTE_BESTANDEN)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((sleutels) => Promise.all(sleutels.filter((k) => k !== VERSIE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const verzoek = e.request;
  // Alleen gewone GET-verzoeken naar deze site; formulieren nooit onderscheppen.
  if (verzoek.method !== "GET" || new URL(verzoek.url).origin !== self.location.origin) return;

  const isVastBestand = new URL(verzoek.url).pathname.startsWith("/static/");

  if (isVastBestand) {
    // Cache first: deze bestanden veranderen zelden.
    e.respondWith(
      caches.match(verzoek).then((gevonden) =>
        gevonden || fetch(verzoek).then((antwoord) => {
          const kopie = antwoord.clone();
          if (antwoord.ok) caches.open(VERSIE).then((c) => c.put(verzoek, kopie));
          return antwoord;
        })
      )
    );
    return;
  }

  // Network first: standen en uitslagen moeten actueel zijn.
  e.respondWith(
    fetch(verzoek)
      .then((antwoord) => {
        const kopie = antwoord.clone();
        if (antwoord.ok) caches.open(VERSIE).then((c) => c.put(verzoek, kopie));
        return antwoord;
      })
      .catch(() => caches.match(verzoek).then((gevonden) => gevonden || caches.match("/")))
  );
});
