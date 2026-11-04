// One page per program: state, configuration summary, health checks, controls and (with monitoring) its resource history.
import { post, get } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, badge, pill, kv, table, toast } from "../ui.js";
import * as state from "../state.js";
import { featureOn } from "../shared.js";
import { lineChart } from "../chart.js";
import { bytes, pct, seconds, clock } from "../format.js";
import { control } from "./infra.js";
import { t } from "../i18n.js";

const NAMES = { postgis: "PostgreSQL / PostGIS", geoserver: "GeoServer", qgis_server: "QGIS Server", mapproxy: "MapProxy", webmap: "ვებ რუკები", caddy: "Caddy", tomcat: "Tomcat" };

// pure: the few settings that matter for each program (never passwords)
export function configSummary(id, cfg) {
  const c = cfg || {};
  const pg = c.postgres || {}, gs = c.geoserver || {}, tc = c.tomcat || {}, q = c.qgis || {}, mp = c.mapproxy || {}, wm = c.webmap || {};
  switch (id) {
    case "postgis": return [["ვერსია", pg.version], ["ბაზა", pg.db], ["მომხმარებელი", pg.user], ["პორტი", pg.port], ["გაფართოებები", (pg.extensions || []).join(", ")], ["დაშვება გარედან", pg.remote_access ? "დიახ" : "არა"], ["max_connections", pg.max_connections]];
    case "geoserver": return [["არხი", gs.channel], ["ვერსია", gs.version], ["heap", gs.heap_mb ? gs.heap_mb + " MB" : ""], ["გაფართოებები", (gs.extensions || []).join(", ")], ["Tomcat პორტი", tc.port]];
    case "qgis_server": return [["პროექტების საქაღალდე", q.projects_dir], ["პროცესები", q.processes]];
    case "mapproxy": return [["გრიდები", (mp.grids || []).join(", ")], ["ქეშის ტიპი", mp.cache_type], ["ფორმატი", mp.format], ["ფენები", (mp.layers || []).map((l) => l.name).join(", ")], ["workers", mp.workers], ["demo", mp.demo ? "ჩართულია" : "გამორთულია"]];
    case "webmap": return [["სათაური", wm.title_ka], ["Title", wm.title_en], ["ბაზისური რუკა", wm.basemap], ["ცენტრი", (wm.center || []).join(", ")], ["მასშტაბი", wm.zoom]];
    default: return [];
  }
}

export default {
  title: (arg) => NAMES[arg] || "სერვისი",
  async mount(root, { arg }) {
    const id = arg || "postgis";
    const head = h("div", { class: "page-head" });
    const body = h("div", { class: "grid cols-2" });
    const log = h("pre", { class: "log", hidden: true, "aria-label": "ოპერაციის ლოგი" });
    append(root, [head, body, log]);

    async function act(action) {
      log.hidden = false; log.textContent = "";
      root.querySelectorAll("button[data-act]").forEach((b) => { b.disabled = true; });
      try { const ok = await control(action, [id], log); toast(ok ? t("დასრულდა") : t("ვერ დასრულდა"), ok ? "ok" : "bad"); }
      catch (e) { toast(e.message, "bad"); }
      await load();
    }

    async function load() {
      let status;
      try { status = await post("/api/v1/ops/status", state.ctx({ components: [id] })); }
      catch (e) { errorBox(body, e, load); return; }
      const row = status.rows.find((r) => r.id === id);
      clear(head); clear(body);
      if (!row) { empty(body, "ეს პროგრამა არჩეულ კონფიგურაციაში არ არის"); return; }
      append(head, [h("div", {}, h("h1", {}, t(NAMES[id] || id), " ", badge(row.state)), h("p", { class: "muted" }, row.detail || "")),
        h("div", { class: "actions" }, ...[["start", "გაშვება"], ["stop", "გაჩერება"], ["restart", "გადატვირთვა"]].map(([a, l]) => h("button", { type: "button", class: "btn", "data-act": a, onclick: () => act(a) }, l)),
          h("a", { class: "btn", href: "#/logs?c=" + id }, "ლოგები"))]);
      const urls = (row.urls || []).map((u) => h("div", {}, h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, u)));
      append(body, [
        h("section", { class: "card", "aria-labelledby": "s-state" }, h("h2", { id: "s-state" }, "მდგომარეობა"),
          kv([["unit", h("span", { class: "mono" }, row.unit)], ["PID", row.pid], ["დაწყებული", row.since], ["გადატვირთვები", row.restarts],
            ["ავტოგაშვება", row.enabled === true ? "დიახ" : row.enabled === false ? "არა" : ""], ["მისამართები", row.listen.length ? h("span", { class: "mono" }, ...row.listen.map((x) => h("div", {}, x))) : ""],
            ["ბმულები", urls.length ? h("span", {}, ...urls) : ""]])),
        h("section", { class: "card", "aria-labelledby": "s-cfg" }, h("h2", { id: "s-cfg" }, "კონფიგურაცია"),
          kv(configSummary(id, state.getConfig()).map(([k, v]) => [k, v === undefined || v === null ? "" : String(v)])),
          h("p", { class: "muted" }, "შესაცვლელად გამოიყენე კლასიკური ინსტალერი (პროგრამის ზოლი: შენახვა და გამოყენება).")),
        h("section", { class: "card", "aria-labelledby": "s-chk" }, h("h2", { id: "s-chk" }, "შემოწმებები"),
          row.checks.length ? table(["შემოწმება", "პასუხი", "შედეგი"], row.checks.map((c) => [c.label, h("span", { class: "mono" }, c.code), c.ok ? pill("ok", "✔ კარგია") : pill("bad", "✖ ვერ გაიარა")])) : h("p", { class: "muted" }, "ამ პროგრამას ავტომატური შემოწმება არ აქვს")),
      ]);
      if (featureOn("monitoring")) body.appendChild(await metrics(id));
    }

    async function metrics(comp) {
      const card = h("section", { class: "card", "aria-labelledby": "s-met" }, h("h2", { id: "s-met" }, "რესურსები (ბოლო 6 საათი)"));
      const since = 6 * 3600;
      try {
        const q = state.query({ since, step: 60 });
        const [cpu, rss] = await Promise.all([
          get("/api/v1/metrics/history", { ...q, name: "service_cpu_percent", labels: JSON.stringify({ service: comp }) }),
          get("/api/v1/metrics/history", { ...q, name: "service_rss_bytes", labels: JSON.stringify({ service: comp }) })]);
        const c1 = h("div", {}), c2 = h("div", {});
        const ok1 = lineChart(c1, [{ name: "CPU", cls: "c1", points: cpu }], { label: "CPU", fmt: (v) => pct(v), tfmt: clock, min: 0 });
        const ok2 = lineChart(c2, [{ name: "RAM", cls: "c2", points: rss }], { label: "RAM", fmt: bytes, tfmt: clock, min: 0 });
        if (!ok1 && !ok2) append(card, h("p", { class: "muted" }, "ისტორია ჯერ არ არის — გაუშვი: gisstack monitor watch"));
        else append(card, [h("h3", {}, "CPU (ერთი ბირთვის %)"), c1, h("h3", {}, "მეხსიერება"), c2]);
        if (comp === "mapproxy") {
          const cache = await get("/api/v1/metrics/history", { ...q, name: "mapproxy_cache_bytes" });
          if (cache.length) append(card, [h("h3", {}, "ქეშის ზომა"), (() => { const d = h("div", {}); lineChart(d, [{ name: "cache", cls: "c3", points: cache }], { label: "cache", fmt: bytes, tfmt: clock, min: 0 }); return d; })()]);
        }
        if (comp === "postgis") {
          const conn = await get("/api/v1/metrics/history", { ...q, name: "pg_connections_percent" });
          if (conn.length) append(card, [h("h3", {}, "კავშირები (% max_connections-დან)"), (() => { const d = h("div", {}); lineChart(d, [{ name: "connections", cls: "c4", points: conn }], { label: "connections", fmt: (v) => pct(v), tfmt: clock, min: 0, max: 100 }); return d; })()]);
        }
      } catch (e) { append(card, h("p", { class: "muted" }, e.code === "not_found" ? "ისტორია ჯერ არ არის — გაუშვი: gisstack monitor watch" : e.message)); }
      return card;
    }

    loading(body, 3);
    await load();
    return { refresh: load };
  },
};
