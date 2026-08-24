/* Shuss League — front-end gedrag */
(function () {
  "use strict";

  /* Tabs (Spelers / Teams op de klassementpagina) */
  document.querySelectorAll("[data-tabs]").forEach(function (groep) {
    groep.querySelectorAll(".tab").forEach(function (knop) {
      knop.addEventListener("click", function () {
        groep.querySelectorAll(".tab").forEach(function (k) { k.classList.remove("actief"); });
        knop.classList.add("actief");
        document.querySelectorAll("[data-paneel]").forEach(function (p) {
          p.classList.toggle("verborgen", p.dataset.paneel !== knop.dataset.tab);
        });
      });
    });
  });

  /* Live zoeken in een lijst (data-zoek = CSS-selector van de tabel of het vak).
     Bij een tabel worden de rijen gefilterd, anders de directe kinderen. */
  document.querySelectorAll("input[data-zoek]").forEach(function (veld) {
    var tabel = null;
    try { tabel = document.querySelector(veld.dataset.zoek); } catch (e) { return; }
    if (!tabel) return;
    var rijen = tabel.tagName === "TABLE"
      ? tabel.querySelectorAll("tbody tr")
      : tabel.children;
    veld.addEventListener("input", function () {
      var term = veld.value.trim().toLowerCase();
      Array.prototype.forEach.call(rijen, function (rij) {
        /* Gezocht wordt in de naamkolom en in alles wat als .zoekbaar gemarkeerd
           is (bv. de teamleden, zodat je een team ook via een speler vindt). */
        var velden = rij.querySelectorAll(".naam, .zoekbaar");
        var tekst = Array.prototype.map.call(velden, function (el) {
          return el.textContent;
        }).join(" ").toLowerCase();
        rij.style.display = (!term || tekst.indexOf(term) !== -1) ? "" : "none";
      });
    });
  });

  /* Kleuren en balkbreedtes die uit de database komen.
     Ze staan als data-attribuut in de HTML (en niet in een style="..."), zodat de
     sjablonen geldige HTML blijven en editors er geen valse CSS-fouten in zien. */
  document.querySelectorAll("[data-kleur]").forEach(function (el) {
    var kleur = el.dataset.kleur || "";
    if (/^#[0-9a-f]{3,8}$/i.test(kleur.trim())) el.style.background = kleur.trim();
  });
  document.querySelectorAll("[data-pct]").forEach(function (el) {
    var pct = parseFloat(el.dataset.pct);
    if (!isNaN(pct)) el.style.width = Math.max(0, Math.min(100, pct)) + "%";
  });

  /* Keuzelijst filteren (bv. teamgenoot zoeken op naam of spelersnummer) */
  document.querySelectorAll("[data-filter-doel]").forEach(function (veld) {
    var doel = null;
    try { doel = document.querySelector(veld.dataset.filterDoel); } catch (e) { return; }
    if (!doel) return;
    var teller = document.getElementById(veld.id.replace("-zoek", "-teller"));
    var alle = Array.prototype.slice.call(doel.options).map(function (o) {
      return { waarde: o.value, tekst: o.text, zoek: (o.dataset.term || o.text).toLowerCase(),
               uit: o.disabled };
    });

    function herteken() {
      var term = veld.value.trim().toLowerCase();
      var treffers = alle.filter(function (o) {
        return o.uit || !term || o.zoek.indexOf(term) !== -1;
      });
      doel.innerHTML = "";
      treffers.forEach(function (o) {
        var optie = document.createElement("option");
        optie.value = o.waarde;
        optie.textContent = o.tekst;
        optie.disabled = o.uit;
        doel.appendChild(optie);
      });
      var aantal = treffers.filter(function (o) { return !o.uit; }).length;
      if (teller) {
        teller.textContent = !term ? "" : (aantal === 0
          ? "Geen speler gevonden — probeer een deel van de naam of het nummer."
          : aantal + (aantal === 1 ? " speler gevonden" : " spelers gevonden"));
      }
      if (aantal === 1) {
        var enige = treffers.filter(function (o) { return !o.uit; })[0];
        doel.value = enige.waarde;
      }
    }

    veld.addEventListener("input", herteken);
  });

  /* De site op het startscherm zetten.
     Android/Chrome geeft ons een echte installatieknop via 'beforeinstallprompt'.
     Safari op iPhone kent dat niet: daar moet het via het deelmenu, dus tonen we
     enkel een korte uitleg. Draait de site al als app, dan zwijgen we. */
  (function () {
    var balk = document.getElementById("installeer");
    if (!balk) return;

    var alApp = window.matchMedia("(display-mode: standalone)").matches
      || window.navigator.standalone === true;
    if (alApp) return;

    try {
      if (window.localStorage.getItem("installeer-weg") === "1") return;
    } catch (e) { /* privémodus: dan tonen we ze gewoon */ }

    var tekst = document.getElementById("installeer-tekst");
    var knop = document.getElementById("installeer-knop");
    var weg = document.getElementById("installeer-weg");
    var bewaard = null;

    function toon(boodschap, metKnop) {
      tekst.textContent = boodschap;
      knop.hidden = !metKnop;
      balk.hidden = false;
    }

    weg.addEventListener("click", function () {
      balk.hidden = true;
      try { window.localStorage.setItem("installeer-weg", "1"); } catch (e) {}
    });

    /* Android, Chrome, Edge … */
    window.addEventListener("beforeinstallprompt", function (e) {
      e.preventDefault();
      bewaard = e;
      toon("📲 Zet Lebershuss op je startscherm — dan opent het als een app.", true);
    });

    knop.addEventListener("click", function () {
      if (!bewaard) return;
      bewaard.prompt();
      bewaard.userChoice.then(function () {
        bewaard = null;
        balk.hidden = true;
      });
    });

    window.addEventListener("appinstalled", function () {
      balk.hidden = true;
      try { window.localStorage.setItem("installeer-weg", "1"); } catch (e) {}
    });

    /* iPhone en iPad: geen knop mogelijk, dus uitleggen hoe het wél gaat. */
    var isIOS = /iphone|ipad|ipod/i.test(window.navigator.userAgent)
      || (window.navigator.platform === "MacIntel" && window.navigator.maxTouchPoints > 1);
    if (isIOS) {
      toon("📲 Op je startscherm zetten? Tik op het deel-icoon onderaan en kies "
           + "“Zet op beginscherm”.", false);
    }
  })();

  /* Bevestiging vragen bij gevoelige formulieren */
  document.querySelectorAll("form[data-confirm]").forEach(function (formulier) {
    formulier.addEventListener("submit", function (e) {
      if (!window.confirm(formulier.dataset.confirm)) e.preventDefault();
    });
  });
})();

/* ELO-grafiek op spelersprofielen (vanilla SVG, geen externe libraries) */
(function () {
  "use strict";
  var houder = document.getElementById("elo-grafiek");
  if (!houder || !window.ELO_PUNTEN || window.ELO_PUNTEN.length < 2) return;

  var data = window.ELO_PUNTEN;
  var B = 640, H = 260, padL = 46, padR = 16, padT = 16, padB = 30;
  var elos = data.map(function (p) { return p.elo; });
  var minE = Math.min.apply(null, elos), maxE = Math.max.apply(null, elos);
  var marge = Math.max(20, (maxE - minE) * 0.15);
  minE -= marge; maxE += marge;

  function x(i) { return padL + (B - padL - padR) * (data.length === 1 ? 0.5 : i / (data.length - 1)); }
  function y(e) { return padT + (H - padT - padB) * (1 - (e - minE) / (maxE - minE)); }

  var ns = "http://www.w3.org/2000/svg";
  var svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 " + B + " " + H);

  /* y-as met 4 hulplijnen */
  for (var t = 0; t <= 3; t++) {
    var ew = minE + (maxE - minE) * t / 3;
    var lijn = document.createElementNS(ns, "line");
    lijn.setAttribute("x1", padL); lijn.setAttribute("x2", B - padR);
    lijn.setAttribute("y1", y(ew)); lijn.setAttribute("y2", y(ew));
    lijn.setAttribute("class", "elo-as");
    svg.appendChild(lijn);
    var lbl = document.createElementNS(ns, "text");
    lbl.setAttribute("x", padL - 8); lbl.setAttribute("y", y(ew) + 4);
    lbl.setAttribute("text-anchor", "end"); lbl.setAttribute("class", "elo-aslabel");
    lbl.textContent = Math.round(ew);
    svg.appendChild(lbl);
  }

  /* gevuld vlak + lijn */
  var lijnPunten = data.map(function (p, i) { return x(i) + "," + y(p.elo); }).join(" ");
  var vlak = document.createElementNS(ns, "polygon");
  vlak.setAttribute("points", lijnPunten + " " + x(data.length - 1) + "," + y(minE) + " " + x(0) + "," + y(minE));
  vlak.setAttribute("class", "elo-vlak");
  svg.appendChild(vlak);
  var poly = document.createElementNS(ns, "polyline");
  poly.setAttribute("points", lijnPunten);
  poly.setAttribute("class", "elo-lijn");
  svg.appendChild(poly);

  /* punten */
  var cirkels = data.map(function (p, i) {
    var c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", x(i)); c.setAttribute("cy", y(p.elo)); c.setAttribute("r", 3.5);
    c.setAttribute("class", "elo-punt");
    svg.appendChild(c);
    return c;
  });

  /* x-aslabels: eerste en laatste wedstrijd */
  [[0, "start"], [data.length - 1, "end"]].forEach(function (paar) {
    var lbl = document.createElementNS(ns, "text");
    lbl.setAttribute("x", x(paar[0])); lbl.setAttribute("y", H - 8);
    lbl.setAttribute("text-anchor", paar[1] === "start" ? "start" : "end");
    lbl.setAttribute("class", "elo-aslabel");
    lbl.textContent = data[paar[0]].datum;
    svg.appendChild(lbl);
  });

  houder.appendChild(svg);
  var tip = document.createElement("div");
  tip.className = "elo-tooltip";
  houder.appendChild(tip);

  function toonPunt(i, clientX) {
    cirkels.forEach(function (c, j) { c.classList.toggle("actief", i === j); });
    var p = data[i];
    tip.innerHTML = p.datum + "<br><strong>" + p.elo + " ELO</strong> · #" + p.rang + " in het klassement";
    var rect = houder.getBoundingClientRect();
    var schaal = rect.width / B;
    tip.style.left = (x(i) * schaal) + "px";
    tip.style.top = (y(p.elo) * schaal) + "px";
    tip.style.display = "block";
  }

  svg.addEventListener("mousemove", function (e) {
    var rect = houder.getBoundingClientRect();
    var mx = (e.clientX - rect.left) / (rect.width / B);
    var beste = 0, afstand = Infinity;
    data.forEach(function (_, i) {
      var d = Math.abs(x(i) - mx);
      if (d < afstand) { afstand = d; beste = i; }
    });
    toonPunt(beste, e.clientX);
  });
  svg.addEventListener("mouseleave", function () {
    tip.style.display = "none";
    cirkels.forEach(function (c) { c.classList.remove("actief"); });
  });
})();
