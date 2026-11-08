// PostGIS manager: health and inventory, a read-only SQL editor (dedicated role, one statement at a time, paged) and an explicit, time-boxed write mode.
// Everything goes through the /api/v1/postgis routes; the database role and the read-only transaction are the safety boundary, not this page.
import { post } from "../api.js";
import { h, append, clear, toast, loading, done, errorBox, pill, table, kv, typedConfirm } from "../ui.js";
import * as state from "../state.js";

const call = (path, extra) => post(path, state.ctx(extra));
const fmtBytes = (n) => (n >= 1073741824 ? (n / 1073741824).toFixed(1) + " GB" : n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? Math.round(n / 1024) + " kB" : n + " B");
const cell = (v) => (v === null ? h("em", { class: "muted" }, "NULL") : String(v));

export default {
  title: () => "PostGIS-ის მენეჯერი",
  async mount(root) {
    const body = h("div", { class: "stack" });
    let elevation = null, timer = null;
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "PostGIS-ის მენეჯერი"), h("p", { class: "muted" }, state.describe()))), body]);

    async function refresh() {
      clear(body); loading(body, 4);
      let inv, health;
      try { [inv, health] = await Promise.all([call("/api/v1/postgis/inventory"), call("/api/v1/postgis/health")]); }
      catch (e) { done(body); clear(body); errorBox(body, e, refresh); return; }
      done(body); clear(body);
      const s = inv.server;
      append(body, [
        h("section", { class: "card" }, h("h2", {}, "მდგომარეობა"),
          h("p", {}, pill(health.ok ? "ok" : "bad", health.ok ? "✔ მუშაობს" : "✖ პრობლემაა"), " ", `PostgreSQL ${s.version}`),
          kv([["ბაზა", inv.database], ["კავშირები", `${s.connections} / ${s.max_connections}`], ["ქეშის დარტყმა", s.cache_hit_percent === null ? "" : s.cache_hit_percent + " %"],
              ["გაშვებულია", s.started], ["გაფართოებები", inv.extensions.map((e) => `${e.name} ${e.version}`).join(", ")]]),
          health.findings.length ? table(["დონე", "რა", "შეტყობინება"], health.findings.map((f) => [f.level, f.what, f.message])) : null),
        h("section", { class: "card" }, h("h2", {}, "სივრცითი სვეტები"),
          table(["სვეტი", "ტიპი", "SRID", "ინდექსი"], inv.spatial.map((c) => [`${c.schema}.${c.table}.${c.column}`, c.type, String(c.srid), pill(c.spatial_index ? "ok" : "warn", c.spatial_index ? "✔ GiST" : "არ არის")]),
            { empty: "სივრცითი სვეტი არ არის" })),
        h("section", { class: "card" }, h("h2", {}, "სქემები და ცხრილები"),
          table(["სქემა", "ცხრილები", "ზომა"], inv.schemas.map((x) => [x.name, String(x.tables), fmtBytes(x.bytes)]), { empty: "ცხრილი არ არის" }),
          table(["ცხრილი", "სახეობა", "სავარაუდო ჩანაწერები", "ზომა"], inv.tables.slice(0, 25).map((t) => [`${t.schema}.${t.name}`, t.kind, String(t.rows_estimate), fmtBytes(t.bytes)]))),
        h("section", { class: "card" }, h("h2", {}, "ბაზები"), table(["ბაზა", "ზომა", "კოდირება"], inv.databases.map((d) => [d.name, fmtBytes(d.bytes), d.encoding]))),
        inv.statements ? h("section", { class: "card" }, h("h2", {}, "ყველაზე ნელი მოთხოვნები"),
          table(["მოთხოვნა", "გამოძახებები", "სულ, მწმ", "საშუალო, მწმ"], inv.statements.map((q) => [q.query, String(q.calls), String(q.total_ms), String(q.mean_ms)]))) : null,
        editorCard(),
      ]);
      Object.entries(inv.errors).forEach(([k, m]) => toast(`${k}: ${m}`, "bad"));
    }

    function editorCard() {
      const sql = h("textarea", { id: "pg-sql", rows: "6", spellcheck: "false", "aria-label": "SQL", placeholder: "select * from …" });
      const limit = h("select", { id: "pg-limit", "aria-label": "ჩანაწერები" }, ...[100, 500, 1000, 5000].map((n) => h("option", { value: n, selected: n === 1000 }, String(n))));
      const status = h("p", { class: "muted", role: "status" });
      const out = h("div", {});
      const modeBar = h("div", { class: "row" });
      let offset = 0;

      const run = h("button", { type: "button", class: "btn primary", onclick: () => go(0) }, "გაშვება (მხოლოდ კითხვა)");
      sql.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); go(0); } });

      async function go(off) {
        offset = off; status.textContent = "…"; clear(out);
        try {
          const r = await call("/api/v1/postgis/query", { sql: sql.value, limit: Number(limit.value), offset: off });
          show(r);
        } catch (e) {
          status.textContent = "";
          if (e.code === "not_ready") append(out, [h("p", {}, e.message), h("button", { type: "button", class: "btn", onclick: setup }, "მხოლოდ-წაკითხვის როლის მომზადება")]);
          else append(out, h("p", { class: "error", role: "alert" }, e.message));
        }
      }
      function show(r) {
        status.textContent = `${r.row_count} ჩანაწერი · ${r.elapsed_ms} მწმ` + (r.truncated ? " · შეკვეცილია (ზომის ლიმიტი)" : "") + (r.tag ? " · " + r.tag : "");
        clear(out);
        append(out, table(r.columns, r.rows.map((row) => row.map(cell)), { empty: "ჩანაწერი არ არის" }));
        if (r.paged && (r.has_more || r.offset > 0)) {
          append(out, h("div", { class: "row" },
            h("button", { type: "button", class: "btn", disabled: r.offset === 0, onclick: () => go(Math.max(0, offset - Number(limit.value))) }, "← წინა"),
            h("button", { type: "button", class: "btn", disabled: !r.has_more, onclick: () => go(offset + Number(limit.value)) }, "შემდეგი →")));
        }
      }
      async function setup() {
        try { await call("/api/v1/postgis/editor/setup", {}); toast("როლი მზადაა", "ok"); await go(0); } catch (e) { toast(e.message, "bad"); }
      }

      // ---- write mode
      const write = h("button", { type: "button", class: "btn danger", onclick: runWrite, disabled: true }, "გაშვება (ცვლის მონაცემებს)");
      const toggle = h("button", { type: "button", class: "btn", onclick: enable }, "ჩაწერის რეჟიმის ჩართვა…");
      async function enable() {
        if (elevation) { await disable(); return; }
        if (!(await typedConfirm("ჩაწერის რეჟიმში მოთხოვნებს შეუძლია მონაცემების შეცვლა და წაშლა. ყოველი ბრძანება ჩაიწერება აუდიტში. რეჟიმი თვითონ გაითიშება 5 წუთში.", "WRITE", { ok: "ჩართვა" }))) return;
        try {
          const r = await call("/api/v1/postgis/elevate", { confirm: true, minutes: 5 });
          elevation = { id: r.elevation, until: r.expires * 1000 };
          paintMode(); timer = setInterval(paintMode, 1000);
        } catch (e) { toast(e.message, "bad"); }
      }
      async function disable() {
        const id = elevation && elevation.id;
        elevation = null; clearInterval(timer); paintMode();
        if (id) { try { await call("/api/v1/postgis/elevate/revoke", { elevation: id }); } catch (e) { /* already gone */ } }
      }
      function paintMode() {
        if (elevation && Date.now() >= elevation.until) { elevation = null; clearInterval(timer); }
        clear(modeBar);
        write.disabled = !elevation;
        toggle.textContent = elevation ? "ჩაწერის რეჟიმის გამორთვა" : "ჩაწერის რეჟიმის ჩართვა…";
        append(modeBar, [toggle, write, elevation ? pill("warn", `⚠ ჩაწერა ჩართულია · ${Math.max(0, Math.ceil((elevation.until - Date.now()) / 1000))} წმ`) : pill("ok", "✔ მხოლოდ კითხვა")]);
      }
      async function runWrite() {
        if (!elevation) return;
        status.textContent = "…"; clear(out);
        try { show(await call("/api/v1/postgis/execute", { sql: sql.value, elevation: elevation.id, confirm: true })); }
        catch (e) { status.textContent = ""; append(out, h("p", { class: "error", role: "alert" }, e.message)); }
      }
      paintMode();
      return h("section", { class: "card stack" }, h("h2", {}, "SQL რედაქტორი"),
        h("p", { class: "muted" }, "ერთი ბრძანება; ნაგულისხმევად მხოლოდ კითხვა (ცალკე შეზღუდული როლი, read-only ტრანზაქცია, დროის ლიმიტი)."),
        sql, h("div", { class: "row" }, run, h("label", { for: "pg-limit" }, "ჩანაწერები"), limit), modeBar, status, out);
    }

    await refresh();
    return { refresh, destroy() { clearInterval(timer); elevation = null; } };
  },
};
