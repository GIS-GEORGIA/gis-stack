/* gisstack bilingual web map (Leaflet). Layers and view come from config.js. */
(function () {
  "use strict";
  var CFG = window.GISSTACK_CONFIG;

  var I18N = {
    ka: { baseLayers: "ფონი", overlays: "ფენები", noBase: "ფონის გარეშე", reset: "საწყისი ხედი",
          coords: "კოორდინატები", tileError: "ფენის ჩატვირთვა ვერ მოხერხდა" },
    en: { baseLayers: "Base map", overlays: "Layers", noBase: "No base map", reset: "Reset view",
          coords: "Coordinates", tileError: "Could not load layer" }
  };

  function safeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function safeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }

  var qs = new URLSearchParams(location.search);
  var lang = qs.get("lang") || safeGet("gisstack.lang") || CFG.lang || "ka";
  if (!I18N[lang]) lang = "ka";

  var map = L.map("map", { zoomControl: true, attributionControl: true })
    .setView(CFG.center, CFG.zoom);
  L.control.scale({ imperial: false }).addTo(map);

  var baseLayers = {}, overlays = {}, control = null, layerObjs = [];

  function makeBase() {
    if (!CFG.basemap) return null;
    var o = { attribution: CFG.basemap.attr, maxZoom: 19 };
    if (CFG.basemap.subdomains) o.subdomains = CFG.basemap.subdomains;
    return L.tileLayer(CFG.basemap.url, o);
  }

  function makeOverlay(def) {
    var layer;
    if (def.type === "wms") {
      layer = L.tileLayer.wms(def.url, {
        layers: def.layers, format: def.opaque ? "image/jpeg" : "image/png",
        transparent: !def.opaque, version: "1.1.1"
      });
    } else {
      layer = L.tileLayer(def.url, { maxZoom: 22, maxNativeZoom: 19 });
    }
    layer.on("tileerror", function () {
      document.getElementById("status").classList.add("err");
      document.getElementById("coords").textContent = I18N[lang].tileError + ": " + def.id;
    });
    return layer;
  }

  var base = makeBase();
  if (base) base.addTo(map);
  CFG.layers.forEach(function (def, i) {
    var l = makeOverlay(def);
    layerObjs.push({ def: def, layer: l });
    if (i === 0) l.addTo(map);
  });

  function buildControl() {
    if (control) map.removeControl(control);
    var t = I18N[lang];
    var bl = {};
    if (base) bl[t.baseLayers] = base;
    var ov = {};
    layerObjs.forEach(function (o) { ov[o.def.title[lang] || o.def.title.en] = o.layer; });
    control = L.control.layers(bl, ov, { collapsed: window.innerWidth < 640 }).addTo(map);
  }

  var Reset = L.Control.extend({
    options: { position: "topleft" },
    onAdd: function () {
      var d = L.DomUtil.create("div", "leaflet-bar leaflet-control leaflet-control-reset");
      var a = L.DomUtil.create("a", "", d);
      a.href = "#"; a.innerHTML = "&#8962;"; a.setAttribute("role", "button");
      L.DomEvent.on(a, "click", function (e) {
        L.DomEvent.preventDefault(e);
        map.setView(CFG.center, CFG.zoom);
      });
      this._a = a;
      return d;
    }
  });
  var reset = new Reset().addTo(map);

  map.on("mousemove", function (e) {
    var el = document.getElementById("coords");
    el.parentNode.classList.remove("err");
    el.textContent = I18N[lang].coords + ": " + e.latlng.lat.toFixed(5) + ", " + e.latlng.lng.toFixed(5);
  });

  function applyLang(l) {
    lang = l; safeSet("gisstack.lang", l);
    document.documentElement.lang = l;
    document.title = CFG.title[l] || CFG.title.en;
    document.getElementById("title").textContent = document.title;
    reset._a.title = I18N[l].reset;
    Array.prototype.forEach.call(document.querySelectorAll("#lang button"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-lang") === l);
    });
    buildControl();
  }
  Array.prototype.forEach.call(document.querySelectorAll("#lang button"), function (b) {
    b.addEventListener("click", function () { applyLang(b.getAttribute("data-lang")); });
  });
  applyLang(lang);
})();
