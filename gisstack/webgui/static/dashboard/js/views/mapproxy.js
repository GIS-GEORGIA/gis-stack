// MapProxy manager: health, layers and caches (size, tiles, levels), tile preview, guarded config edits with backups, seeding with an estimate first,
// and cache cleanup (dry run first). Everything goes through the /api/v1/mapproxy routes; destructive steps ask for a typed confirmation.
import { post, postImage, runJob } from "../api.js";
import { h, append, clear, toast, loading, done, errorBox, pill, table, kv, typedConfirm, modal } from "../ui.js";
import * as state from "../state.js";

const call = (path, extra) => post(path, state.ctx(extra));
const fmtBytes = (n) => (n >= 1073741824 ? (n / 1073741824).toFixed(1) + " GB" : n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? Math.round(n / 1024) + " kB" : n + " B");
const num = (el) => Number(el.value);

export default {
  title: () => "MapProxy-ის მენეჯერი",
  async mount(root) {
    const body = h("div", { class: "stack" });
    let urls = [], ctl = null, lastSeedLog = "";                      // the seeding log survives the page refresh that follows a run
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "MapProxy-ის მენეჯერი"), h("p", { class: "muted" }, state.describe()))), body]);

    async function refresh() {
      clear(body); loading(body, 4);
      let cfg, health, cache, backups;
      try { [cfg, health, cache, backups] = await Promise.all([call("/api/v1/mapproxy/config"), call("/api/v1/mapproxy/health"), call("/api/v1/mapproxy/cache"), call("/api/v1/mapproxy/config/backups")]); }
      catch (e) { done(body); clear(body); errorBox(body, e, refresh); return; }
      done(body); clear(body);
      const bySize = Object.fromEntries(cache.caches.map((c) => [c.name, c]));
      append(body, [
        h("section", { class: "card" }, h("h2", {}, "მდგომარეობა"),
          h("p", {}, pill(health.ok ? "ok" : "bad", health.ok ? "✔ მუშაობს" : "✖ პრობლემაა"), " ", health.seeding ? pill("warn", "⚠ მიმდინარეობს დასიდვა") : null),
          kv([["სერვისი", health.service], ["HTTP", String(health.http)], ["ფენები", health.layers_served.join(", ")], ["დისკი", cache.disk_free_kb === null ? "" : fmtBytes(cache.disk_free_kb * 1024) + " / " + fmtBytes(cache.disk_total_kb * 1024)]]),
          health.findings.length ? table(["დონე", "რა", "შეტყობინება"], health.findings.map((f) => [f.level, f.what, f.message])) : null),
        h("section", { class: "card stack" }, h("h2", {}, "ფენები და ქეში"),
          table(["ფენა", "ქეში", "ზომა", "ფილები", ""], cfg.layers.map((l) => {
            const c = bySize[l.sources[0]];
            return [l.name, l.sources.join(", "), c ? fmtBytes(c.bytes) + (c.partial ? " ≈" : "") : "—", c ? String(c.tiles) : "—",
              h("div", { class: "row" }, h("button", { type: "button", class: "btn", onclick: () => preview(l, cfg) }, "ფილის გადახედვა"),
                h("button", { type: "button", class: "btn", onclick: () => removeLayer(l.name) }, "ფენის წაშლა"),
                c ? h("button", { type: "button", class: "btn danger", onclick: () => clean(l.sources[0]) }, "ქეშის გასუფთავება") : null)];
          }), { empty: "ფენა არ არის" }),
          cache.unmanaged.length ? h("p", { class: "muted" }, "კონფიგურაციაში არარსებული ქეშის საქაღალდეები: " + cache.unmanaged.map((u) => `${u.entry} (${fmtBytes(u.bytes)})`).join(", ")) : null),
        seedCard(cfg),
        addLayerCard(cfg),
        h("section", { class: "card stack" }, h("h2", {}, "წყაროები და ბადეები"),
          table(["წყარო", "ტიპი", "მისამართი"], cfg.sources.map((s) => [s.name, s.type, s.url]), { empty: "წყარო არ არის" }),
          table(["ბადე", "SRS"], cfg.grids.map((g) => [g.name, g.srs]))),
        h("section", { class: "card stack" }, h("h2", {}, "კონფიგურაციის ასლები"),
          h("p", { class: "muted" }, "ყოველი ცვლილების წინ ავტომატურად ინახება. შენიშვნა: gisstack deploy/apply ფაილს პროექტიდან თავიდან ქმნის."),
          table(["ასლი", "ზომა", ""], backups.map((b) => [b.id, fmtBytes(b.bytes), h("button", { type: "button", class: "btn", onclick: () => restore(b.id) }, "აღდგენა")]), { empty: "ასლი ჯერ არ არის" })),
      ]);
    }

    async function preview(layer, cfg) {
      const cache = cfg.caches.find((c) => c.name === layer.sources[0]);
      const grid = h("select", { id: "mp-grid", "aria-label": "grid" }, ...(cache ? cache.grids : ["GLOBAL_WEBMERCATOR"]).map((g) => h("option", { value: g }, g)));
      const z = h("input", { type: "number", id: "mp-z", min: "0", max: "24", value: "0", "aria-label": "z" }), x = h("input", { type: "number", id: "mp-x", min: "0", value: "0", "aria-label": "x" }), y = h("input", { type: "number", id: "mp-y", min: "0", value: "0", "aria-label": "y" });
      const img = h("img", { alt: layer.name, width: "256", height: "256", class: "preview" }), status = h("p", { class: "muted" }, "");
      const load = h("button", { type: "button", class: "btn primary", onclick: async () => {
        status.textContent = "…";
        try { const u = await postImage("/api/v1/mapproxy/tile", state.ctx({ layer: layer.name, grid: grid.value, z: num(z), x: num(x), y: num(y) })); urls.push(u); img.src = u; status.textContent = ""; }
        catch (e) { status.textContent = e.message; }
      } }, "ჩატვირთვა");
      modal({ title: layer.name, body: h("div", { class: "stack" }, h("p", { class: "muted" }, "ფილა, რომელიც ქეშში არ არის, წყაროდან მოიტანება და შეინახება."), h("div", { class: "row" }, grid, z, x, y, load), status, img), actions: [{ label: "დახურვა", value: true, kind: "primary" }] });
    }

    function addLayerCard(cfg) {
      const name = h("input", { type: "text", id: "al-name", pattern: "[A-Za-z][A-Za-z0-9_]*", maxlength: "40", "aria-label": "name" });
      const title = h("input", { type: "text", id: "al-title", maxlength: "120", "aria-label": "title" });
      const kind = h("select", { id: "al-kind", "aria-label": "source" }, h("option", { value: "wms" }, "WMS"), h("option", { value: "tile" }, "Tile"));
      const url = h("input", { type: "url", id: "al-url", placeholder: "http://127.0.0.1:8080/geoserver/wms", "aria-label": "url" });
      const layers = h("input", { type: "text", id: "al-layers", placeholder: "workspace:layer", "aria-label": "layers" });
      const fmt = h("select", { id: "al-fmt", "aria-label": "format" }, ...["png", "jpeg", "mixed"].map((f) => h("option", { value: f }, f)));
      const out = h("div", {});
      const params = () => ({ op: "add_layer", params: { name: name.value.trim(), title: title.value.trim() || name.value.trim(), source: kind.value, url: url.value.trim(), layers: layers.value.trim(), format: fmt.value } });
      const run = async (dry) => {
        clear(out);
        try {
          const r = await call("/api/v1/mapproxy/config/edit", { ...params(), dry_run: dry });
          append(out, h("p", {}, (dry ? "✔ სწორია: " : "✔ შესრულდა: ") + r.changes.join(", ")));
          if (!dry) { toast("კონფიგურაცია შეიცვალა", "ok"); await refresh(); }
        } catch (e) { append(out, h("p", { class: "error", role: "alert" }, e.message)); }
      };
      return h("section", { class: "card stack" }, h("h2", {}, "ფენის დამატება"),
        h("div", { class: "row" }, h("label", { for: "al-name" }, "სახელი"), name, h("label", { for: "al-title" }, "სათაური"), title, h("label", { for: "al-kind" }, "წყარო"), kind, h("label", { for: "al-fmt" }, "ფორმატი"), fmt),
        h("div", { class: "row" }, h("label", { for: "al-url" }, "მისამართი"), url, h("label", { for: "al-layers" }, "WMS ფენები"), layers),
        h("div", { class: "row" }, h("button", { type: "button", class: "btn", onclick: () => run(true) }, "შემოწმება"), h("button", { type: "button", class: "btn primary", onclick: () => run(false) }, "დამატება")), out);
    }

    async function removeLayer(name) {
      if (!(await typedConfirm(`ფენა „${name}“ ამოიშლება კონფიგურაციიდან (ქეშის ფაილები დარჩება). წინასწარ შეიქმნება ასლი.`, name, { ok: "წაშლა" }))) return;
      try { await call("/api/v1/mapproxy/config/edit", { op: "remove_layer", params: { name }, dry_run: false }); toast("წაიშალა", "ok"); await refresh(); } catch (e) { toast(e.message, "bad"); }
    }
    async function restore(id) {
      if (!(await typedConfirm(`კონფიგურაცია დაბრუნდება ასლზე ${id}. მიმდინარე ვერსიაც შეინახება.`, "RESTORE", { ok: "აღდგენა" }))) return;
      try { await call("/api/v1/mapproxy/config/restore", { id, confirm: true }); toast("აღდგა", "ok"); await refresh(); } catch (e) { toast(e.message, "bad"); }
    }

    async function clean(cacheName) {
      let est;
      try { est = await call("/api/v1/mapproxy/cache/cleanup", { cache: cacheName, dry_run: true }); } catch (e) { toast(e.message, "bad"); return; }
      if (!est.files) { toast("ქეში ცარიელია", "info"); return; }
      const ok = await typedConfirm(`წაიშლება ${est.files} ფილა (${fmtBytes(est.bytes)}) ქეშიდან „${cacheName}“. ეს არ უქმდება; ფილები საჭიროებისას თავიდან მოიტანება წყაროდან.`, cacheName, { ok: "გასუფთავება" });
      if (!ok) return;
      try { const r = await call("/api/v1/mapproxy/cache/cleanup", { cache: cacheName, dry_run: false, confirm: true }); toast(`წაიშალა ${r.files} ფილა`, "ok"); await refresh(); } catch (e) { toast(e.message, "bad"); }
    }

    function seedCard(cfg) {
      const cache = h("select", { id: "sd-cache", "aria-label": "cache" }, ...cfg.caches.map((c) => h("option", { value: c.name }, c.name)));
      const from = h("input", { type: "number", id: "sd-from", min: "0", max: "20", value: "0", "aria-label": "from" }), to = h("input", { type: "number", id: "sd-to", min: "0", max: "20", value: "6", "aria-label": "to" });
      const bbox = h("input", { type: "text", id: "sd-bbox", value: "39.9,41.0,46.8,43.7", "aria-label": "bbox" });
      const refreshBox = h("input", { type: "checkbox", id: "sd-refresh" });
      const log = h("pre", { class: "log", role: "log", tabindex: "0", "aria-label": "log" }, lastSeedLog);
      const est = h("button", { type: "button", class: "btn", onclick: () => go(true) }, "შეფასება");
      const run = h("button", { type: "button", class: "btn danger", onclick: () => go(false) }, "დასიდვის დაწყება");
      const stop = h("button", { type: "button", class: "btn", disabled: true, onclick: () => { if (ctl) ctl.abort(); } }, "გაჩერება");
      async function go(dry) {
        const box = bbox.value.split(",").map(Number);
        if (!dry && !(await typedConfirm("დასიდვა ბევრ ფილას ქმნის და წყაროს სერვერს ტვირთავს. ჯერ ჩატარდება შეფასება; ლიმიტს გადაცილებისას გაჩერდება.", "SEED", { ok: "დაწყება" }))) return;
        clear(log); lastSeedLog = ""; ctl = new AbortController(); est.disabled = run.disabled = true; stop.disabled = false;
        try {
          await runJob("/api/v1/mapproxy/seed", state.ctx({ caches: [cache.value], level_from: num(from), level_to: num(to), bbox: box, refresh: refreshBox.checked, dry_run: dry, confirm: !dry }),
            { signal: ctl.signal, interval: 800, onLines: (l) => { log.textContent += l.join("\n") + "\n"; lastSeedLog = log.textContent; log.scrollTop = log.scrollHeight; } });
        } catch (e) { log.textContent += "ERROR: " + e.message + "\n"; lastSeedLog = log.textContent; }
        est.disabled = run.disabled = false; stop.disabled = true;
        if (!dry) await refresh();
      }
      return h("section", { class: "card stack" }, h("h2", {}, "დასიდვა (seeding)"),
        h("p", { class: "muted" }, "ნაგულისხმევად მხოლოდ ის ფილები იქმნება, რომლებიც ჯერ არ არის. „თავიდან აშენება“ ყველას თავიდან მოიტანს."),
        h("div", { class: "row" }, h("label", { for: "sd-cache" }, "ქეში"), cache, h("label", { for: "sd-from" }, "დონე"), from, to, h("label", { for: "sd-bbox" }, "bbox (EPSG:4326)"), bbox,
          h("label", { class: "check", for: "sd-refresh" }, refreshBox, "თავიდან აშენება")),
        h("div", { class: "row" }, est, run, stop), log);
    }

    await refresh();
    return { refresh, destroy() { if (ctl) ctl.abort(); urls.forEach((u) => URL.revokeObjectURL(u)); urls = []; } };
  },
};
