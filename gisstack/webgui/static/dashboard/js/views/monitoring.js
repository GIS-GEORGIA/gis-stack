// Monitoring: history charts (real stored samples), availability, alerts and events. Needs the "monitoring" feature.
import { get, post } from "../api.js";
import { h, append, clear, loading, empty, errorBox, pill, table, toast } from "../ui.js";
import * as state from "../state.js";
import { featureOn } from "../shared.js";
import { lineChart } from "../chart.js";
import { bytes, pct, seconds, clock, dateTime, dateTimeSec, ago } from "../format.js";
import { t } from "../i18n.js";

const RANGES = [["1h", 3600, 0], ["6h", 6 * 3600, 60], ["24h", 86400, 300], ["7d", 7 * 86400, 1800]];
const CLS = ["c1", "c2", "c3", "c4", "c5", "c6"];

// pure: [{ts, labels, value}] -> [{name, cls, points}] grouped by the value of one label (or a single series)
export function groupSeries(points, labelKey) {
  const by = new Map();
  for (const p of points) {
    const k = labelKey ? (p.labels[labelKey] ?? "") : "";
    if (!by.has(k)) by.set(k, []);
    by.get(k).push({ ts: p.ts, value: p.value });
  }
  return [...by.entries()].map(([name, pts], i) => ({ name: name || "", cls: CLS[i % CLS.length], points: pts }));
}

const CHARTS = [
  { id: "cpu", title: "CPU", name: "host_cpu_percent", fmt: (v) => pct(v), min: 0, max: 100 },
  { id: "mem", title: "მეხსიერება", name: "host_mem_used_percent", fmt: (v) => pct(v), min: 0, max: 100 },
  { id: "load", title: "დატვირთვა (load average)", name: "host_load1", fmt: (v) => v.toFixed(2), min: 0 },
  { id: "disk", title: "დისკი", name: "disk_used_percent", by: "mount", fmt: (v) => pct(v), min: 0, max: 100 },
  { id: "pg", title: "PostgreSQL კავშირები", name: "pg_connections_percent", fmt: (v) => pct(v), min: 0, max: 100 },
  { id: "probe", title: "შემოწმებების პასუხის დრო", name: "probe_seconds", by: "probe", fmt: (v) => seconds(v), min: 0 },
  { id: "svc-cpu", title: "სერვისების CPU", name: "service_cpu_percent", by: "service", fmt: (v) => pct(v), min: 0 },
  { id: "svc-mem", title: "სერვისების მეხსიერება", name: "service_rss_bytes", by: "service", fmt: bytes, min: 0 },
  { id: "cache", title: "MapProxy ქეში", name: "mapproxy_cache_bytes", fmt: bytes, min: 0 },
];

export default {
  title: () => "მონიტორინგი",
  async mount(root) {
    if (!featureOn("monitoring")) {
      append(root, [h("h1", {}, "მონიტორინგი"), h("div", { class: "card" }, h("p", {}, "მონიტორინგი გამორთულია."), h("p", { class: "mono" }, "export GISSTACK_FEATURES=dashboard,monitoring"), h("p", { class: "muted" }, "შემდეგ: gisstack monitor watch --mode ssh --ssh root@სერვერი"))]);
      return {};
    }
    let range = RANGES[1];
    const tabs = h("div", { class: "tabs", role: "group", "aria-label": "დროის შუალედი" }, ...RANGES.map((r) => h("button", { type: "button", "aria-pressed": String(r === range), onclick: (e) => { range = r; tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); load(); } }, r[0])));
    const collect = h("button", { type: "button", class: "btn", onclick: collectNow }, "ნიმუშის აღება ახლავე");
    const chartsBox = h("div", { class: "grid cols-2" });
    const side = h("div", { class: "grid cols-2" });
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "მონიტორინგი"), h("p", { class: "muted" }, state.describe())), h("div", { class: "actions" }, tabs, collect)), h("div", { class: "stack" }, side, chartsBox)]);

    async function collectNow() {
      collect.disabled = true;
      try { await post("/api/v1/monitor/collect", state.ctx()); toast(t("ნიმუში აღებულია"), "ok"); await load(); } catch (e) { toast(e.message, "bad"); } finally { collect.disabled = false; }
    }

    async function chartCard(def) {
      const box = h("div", {});
      const card = h("section", { class: "card", "aria-labelledby": "ch-" + def.id }, h("h2", { id: "ch-" + def.id }, def.title), box);
      try {
        const pts = await get("/api/v1/metrics/history", { ...state.query({ name: def.name, since: range[1] }), ...(range[2] ? { step: range[2] } : {}) });
        const ok = lineChart(box, groupSeries(pts, def.by), { label: def.title, fmt: def.fmt, tfmt: range[1] > 86400 ? dateTime : clock, min: def.min, max: def.max });
        if (!ok) return null;                      // no data for this metric on this machine: no empty card
        const dl = h("details", {}, h("summary", {}, "ცხრილის სახით"), (() => {
          const rows = pts.slice(-40).reverse().map((p) => [dateTimeSec(p.ts), Object.values(p.labels).join(" ") || "-", def.fmt(p.value)]);
          return table(["დრო", "ეტიკეტი", "მნიშვნელობა"], rows);
        })());
        card.appendChild(dl);
      } catch (e) { return null; }
      return card;
    }

    async function load() {
      loading(chartsBox, 2);
      try {
        const [snap, al] = await Promise.all([get("/api/v1/metrics", state.query()), get("/api/v1/alerts", state.query())]);
        clear(side);
        const firing = al.alerts.filter((a) => a.state === "firing");
        append(side, [
          h("section", { class: "card", "aria-labelledby": "m-al" }, h("h2", { id: "m-al" }, "ალერტები"),
            firing.length ? h("ul", {}, ...firing.map((a) => h("li", {}, pill(a.rule.includes("down") || a.rule.includes("critical") ? "bad" : "warn", a.rule), " ", a.message, " ", h("span", { class: "muted" }, ago(a.since))))) : h("p", { class: "muted" }, "ალერტები არ არის ✔"),
            h("p", { class: "muted" }, `ბოლო ნიმუში: ${snap.ts ? ago(snap.ts) : "-"}`)),
          h("section", { class: "card pad0", "aria-labelledby": "m-av" }, h("h2", { id: "m-av", style: { padding: ".8rem 1rem 0" } }, "ხელმისაწვდომობა (24 სთ)"),
            table(["სერვისი", "ხელმისაწვდომობა"], Object.entries(snap.availability).map(([k, v]) => [k, pill(v >= 99 ? "ok" : v >= 90 ? "warn" : "bad", pct(v, 1))]), { empty: "მონაცემი არ არის" })),
          h("section", { class: "card pad0", "aria-labelledby": "m-ev", style: { gridColumn: "1 / -1" } }, h("h2", { id: "m-ev", style: { padding: ".8rem 1rem 0" } }, "მოვლენები"),
            table(["დრო", "დონე", "სახეობა", "ობიექტი", "შეტყობინება"], al.events.slice(0, 25).map((e) => [dateTimeSec(e.ts), pill(e.level === "crit" ? "bad" : e.level === "warn" ? "warn" : "gray", e.level), e.kind, h("span", { class: "mono" }, e.subject), e.message]), { empty: "მოვლენები არ არის" })),
        ]);
        const cards = (await Promise.all(CHARTS.map(chartCard))).filter(Boolean);
        clear(chartsBox);
        chartsBox.removeAttribute("aria-busy");
        if (!cards.length) empty(chartsBox, "ამ შუალედში მონაცემი არ არის"); else append(chartsBox, cards);
      } catch (e) {
        clear(side);
        if (e.code === "not_found") empty(chartsBox, "ამ მანქანისთვის ისტორია ჯერ არ არის — დააჭირე „ნიმუშის აღება ახლავე“", null);
        else errorBox(chartsBox, e, load);
      }
    }
    await load();
    return { refresh: load };
  },
};
