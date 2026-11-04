// Overview: health at a glance - service cards, resource gauges, recent operations and alerts. Every number comes from the live status or
// the stored monitoring history; when a source is missing the card says so instead of showing a made-up value.
import { post } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, badge, pill, table, toast } from "../ui.js";
import * as state from "../state.js";
import { featureOn, setDot } from "../shared.js";
import { bytes, pct, ago, duration, dateTime, seconds } from "../format.js";
import { t } from "../i18n.js";

const val = (m, name, labels) => {
  const s = (m[name] || []).find((x) => !labels || Object.entries(labels).every(([k, v]) => x.labels[k] === v));
  return s ? s.value : null;
};
const maxOf = (m, name) => Math.max(-1, ...(m[name] || []).map((x) => x.value));

export function summarize(status, monitoring) {
  // pure: derives the headline from real data. issues = services not running + alerts firing
  const rows = status ? status.rows : [];
  const down = rows.filter((r) => r.state !== "running");
  const failing = rows.filter((r) => r.state === "running" && !r.ok);
  const alerts = monitoring ? monitoring.alerts : [];
  return { total: rows.length, down, failing, alerts, healthy: !!status && !down.length && !failing.length && !alerts.length };
}

function gauge(label, value, text, sub, level) {
  const m = h("meter", { min: 0, max: 100, value: value ?? 0, low: 70, high: 85, optimum: 20, "aria-label": label });
  return h("div", { class: "card metric" }, h("span", { class: "muted" }, label), h("span", { class: "val" }, text), value === null ? null : m, sub ? h("span", { class: "sub" }, sub) : null);
}

function serviceCard(r, m) {
  const sv = { service: r.id };
  const cpu = val(m, "service_cpu_percent", sv), rss = val(m, "service_rss_bytes", sv);
  const okChecks = r.checks.filter((c) => c.ok).length;
  const link = r.urls && r.urls[0] ? h("a", { href: r.urls[0], target: "_blank", rel: "noopener noreferrer" }, r.urls[0].replace(/^https?:\/\//, "")) : null;
  return h("article", { class: "card svc-card" },
    h("h3", {}, h("a", { class: "title", href: "#/service/" + r.id }, r.name), badge(r.state)),
    h("dl", { class: "kv" },
      r.listen[0] ? [h("dt", {}, "მისამართი"), h("dd", { class: "mono" }, r.listen[0])] : null,
      link ? [h("dt", {}, "ბმული"), h("dd", {}, link)] : null,
      r.checks.length ? [h("dt", {}, "შემოწმებები"), h("dd", {}, `${okChecks}/${r.checks.length} `, okChecks === r.checks.length ? "✔" : "✖")] : null,
      cpu !== null || rss !== null ? [h("dt", {}, "რესურსი"), h("dd", {}, `CPU ${pct(cpu)} · ${bytes(rss)}`)] : null,
      r.restarts ? [h("dt", {}, "გადატვირთვები"), h("dd", {}, String(r.restarts))] : null));
}

export default {
  title: () => "მთავარი",
  async mount(root) {
    const banner = h("div", { id: "ov-banner" });
    const cards = h("section", { "aria-labelledby": "ov-svc" }, h("h2", { id: "ov-svc" }, "სერვისები"), h("div", { class: "grid cols-auto", id: "ov-cards" }));
    const gauges = h("section", { "aria-labelledby": "ov-res" }, h("h2", { id: "ov-res" }, "რესურსები"), h("div", { class: "grid cols-auto", id: "ov-gauges" }));
    const lower = h("div", { class: "grid cols-2" },
      h("section", { class: "card pad0", "aria-labelledby": "ov-ops" }, h("h2", { id: "ov-ops", style: { padding: ".8rem 1rem 0" } }, "ბოლო ოპერაციები"), h("div", { id: "ov-opsbox" })),
      h("section", { class: "card", "aria-labelledby": "ov-al" }, h("h2", { id: "ov-al" }, "ალერტები"), h("div", { id: "ov-albox" })));
    const collectBtn = h("button", { type: "button", class: "btn", id: "ov-collect", hidden: !featureOn("monitoring"), onclick: collectNow }, "ნიმუშის აღება ახლავე");
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "მთავარი"), h("p", { class: "muted", id: "ov-sub" })), h("div", { class: "actions" }, collectBtn)), banner, h("div", { class: "stack" }, cards, gauges, lower)]);
    const $c = (id) => root.querySelector("#" + id);

    async function collectNow() {
      collectBtn.disabled = true;
      try { await post("/api/v1/monitor/collect", state.ctx()); toast(t("ნიმუში აღებულია"), "ok"); await load(); }
      catch (e) { toast(e.message, "bad"); } finally { collectBtn.disabled = false; }
    }

    function render(s) {
      const status = s.status, mon = s.monitoring, m = mon ? mon.metrics : {};
      const sum = summarize(status, mon);
      clear(banner);
      if (!status) {
        setDot("bad");
        append(banner, h("div", { class: "banner bad", role: "alert" }, h("span", { class: "big", "aria-hidden": "true" }, "✖"), h("div", {}, h("strong", {}, "მანქანასთან კავშირი ვერ მოხერხდა"), h("div", {}, s.errors.status ? s.errors.status.message : ""))));
      } else if (sum.healthy) {
        setDot("ok");
        append(banner, h("div", { class: "banner ok", role: "status" }, h("span", { class: "big", "aria-hidden": "true" }, "✔"), h("div", {}, h("strong", {}, "ყველაფერი რიგზეა"), h("div", {}, `მუშაობს: ${sum.total} / ${sum.total}`))));
      } else {
        setDot("warn");
        const parts = [];
        if (sum.down.length) parts.push(`არ მუშაობს: ${sum.down.map((r) => r.name).join(", ")}`);
        if (sum.failing.length) parts.push(`შემოწმება ვერ გაიარა: ${sum.failing.map((r) => r.name).join(", ")}`);
        if (sum.alerts.length) parts.push(`აქტიური ალერტები: ${sum.alerts.length}`);
        append(banner, h("div", { class: "banner warn", role: "alert" }, h("span", { class: "big", "aria-hidden": "true" }, "!"), h("div", {}, h("strong", {}, "ყურადღება"), h("div", {}, parts.join(" · ")))));
      }
      $c("ov-sub").textContent = state.describe();
      const cardsBox = $c("ov-cards");
      clear(cardsBox);
      if (status) { done(cardsBox); append(cardsBox, status.rows.map((r) => serviceCard(r, m))); } else empty(cardsBox, "სერვისების მდგომარეობა მიუწვდომელია");

      const g = $c("ov-gauges");
      clear(g);
      if (mon && mon.ts) {
        const cpu = val(m, "host_cpu_percent"), mem = val(m, "host_mem_used_percent"), disk = maxOf(m, "disk_used_percent");
        const pgp = val(m, "pg_connections_percent"), cache = val(m, "mapproxy_cache_bytes");
        const gsUp = (m.probe_up || []).filter((x) => x.labels.probe.startsWith("geoserver:"));
        append(g, [
          cpu !== null ? gauge("CPU", cpu, pct(cpu), null) : null,
          mem !== null ? gauge("მეხსიერება", mem, pct(mem), `${bytes(val(m, "host_mem_total_bytes") - (val(m, "host_mem_available_bytes") || 0))} / ${bytes(val(m, "host_mem_total_bytes"))}`) : null,
          disk >= 0 ? gauge("დისკი", disk, pct(disk), null) : null,
          pgp !== null ? gauge("PostgreSQL კავშირები", pgp, pct(pgp), `${val(m, "pg_connections_total")}/${val(m, "pg_max_connections")}`) : null,
          gsUp.length ? h("div", { class: "card metric" }, h("span", { class: "muted" }, "GeoServer"), h("span", { class: "val" }, gsUp.every((x) => x.value >= 1) ? "✔ ხელმისაწვდომია" : "✖ მიუწვდომელია"),
            h("span", { class: "sub" }, `პასუხი ${seconds(val(m, "probe_seconds", { probe: gsUp[0].labels.probe }))}`)) : null,
          cache !== null ? h("div", { class: "card metric" }, h("span", { class: "muted" }, "MapProxy ქეში"), h("span", { class: "val" }, bytes(cache))) : null,
          val(m, "backup_last_age_seconds") !== null ? h("div", { class: "card metric" }, h("span", { class: "muted" }, "ბოლო ბექაფი"), h("span", { class: "val" }, duration(val(m, "backup_last_age_seconds")) ), h("span", { class: "sub" }, "წინ")) : null,
        ]);
        append(g, h("p", { class: "muted", style: { gridColumn: "1 / -1", margin: 0 } }, `ნიმუში: ${ago(mon.ts)} (${dateTime(mon.ts)})`));
      } else if (featureOn("monitoring")) {
        append(g, h("div", { class: "card", style: { gridColumn: "1 / -1" } }, h("p", {}, "ამ მანქანისთვის მონიტორინგის ისტორია ჯერ არ არსებობს."), h("p", { class: "muted" }, "დააჭირე „ნიმუშის აღება ახლავე“ ან გაუშვი: gisstack monitor watch")));
      } else {
        append(g, h("div", { class: "card", style: { gridColumn: "1 / -1" } }, h("p", {}, "რესურსების მაჩვენებლებისთვის ჩართე მონიტორინგი:"), h("p", { class: "mono" }, "export GISSTACK_FEATURES=dashboard,monitoring")));
      }

      const ops = $c("ov-opsbox");
      clear(ops);
      ops.appendChild(table(["დრო", "ოპერაცია", "შედეგი", "ხანგრძლივობა"], s.operations.map((o) => [dateTime(o.started || o.ended), o.op,
        o.status === "ok" ? pill("ok", "✔ წარმატება") : o.status === "running" ? pill("info", "◌ მიმდინარეობს") : o.status === "aborted" ? pill("warn", "შეწყდა") : pill("bad", "✖ შეცდომა"), duration(o.seconds)]),
        { empty: "ოპერაციები ჯერ არ ყოფილა" }));

      const al = $c("ov-albox");
      clear(al);
      const firing = mon ? mon.alerts : [];
      if (!firing.length) append(al, h("p", { class: "muted" }, mon ? "ალერტები არ არის ✔" : "ალერტები მონიტორინგის გარეშე მიუწვდომელია"));
      else append(al, h("ul", {}, ...firing.map((a) => h("li", {}, pill(a.level === "crit" || /down|critical/.test(a.rule) ? "bad" : "warn", a.rule), " ", a.message, " ", h("span", { class: "muted" }, ago(a.since))))));
      if (s.errors.monitoring) append(al, h("p", { class: "error-box" }, s.errors.monitoring.message));
    }

    async function load() {
      try { render(await post("/api/v1/dashboard/summary", state.ctx())); }
      catch (e) { errorBox(banner, e, load); }
    }
    loading(root.querySelector("#ov-cards"), 2);
    await load();
    return { refresh: load };
  },
};
