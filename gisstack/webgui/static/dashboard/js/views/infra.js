// Infrastructure: every program with its live state and the controls (start / stop / restart / autostart) - the diagnostics of the classic page,
// on the same endpoints (/api/ops/status, /api/ops/control).
import { post, runJob } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, badge, toast, confirmBox } from "../ui.js";
import * as state from "../state.js";
import { dateTime } from "../format.js";
import { t } from "../i18n.js";

const ACTIONS = [["start", "გაშვება"], ["stop", "გაჩერება"], ["restart", "გადატვირთვა"]];

export async function control(action, ids, log) {
  const body = state.ctx({ action, components: ids.length ? ids : null });      // null = the whole stack
  const res = await runJob("/api/v1/ops/control", body, { onLines: (l) => { if (log) { log.textContent += l.join("\n") + "\n"; log.scrollTop = log.scrollHeight; } } });
  return res.ok;
}

export default {
  title: () => "ინფრასტრუქტურა",
  async mount(root) {
    const tableBox = h("div", { class: "card pad0" });
    const log = h("pre", { class: "log", id: "infra-log", "aria-label": "ოპერაციის ლოგი", hidden: true });
    const bulk = h("div", { class: "actions" });
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "ინფრასტრუქტურა"), h("p", { class: "muted" }, state.describe())), bulk), h("div", { class: "stack" }, tableBox, log)]);

    async function act(action, ids, label) {
      if (action === "stop" && (ids.length !== 1 || ids[0] === "postgis") && !(await confirmBox(`${label}: ${ids.join(", ")}. გავაგრძელო?`, { danger: true }))) return;
      log.hidden = false;
      log.textContent = "";
      setBusy(true);
      try {
        const ok = await control(action, ids, log);
        toast(ok ? t("დასრულდა") : t("ვერ დასრულდა"), ok ? "ok" : "bad");
      } catch (e) { toast(e.message, "bad"); }
      finally { setBusy(false); await load(); }
    }
    const setBusy = (b) => root.querySelectorAll("button[data-act]").forEach((x) => { x.disabled = b; });

    function render(status) {
      done(tableBox);
      clear(tableBox);
      clear(bulk);
      append(bulk, ACTIONS.map(([a, label]) => h("button", { type: "button", class: "btn" + (a === "restart" ? " primary" : ""), "data-act": a, onclick: () => act(a, [], `${label} — ყველა`) }, `${label} — ყველა`)));
      const rows = status.rows;
      append(tableBox, h("div", { class: "table-wrap" }, h("table", {},
        h("caption", { class: "sr-only" }, "სერვისების მდგომარეობა"),
        h("thead", {}, h("tr", {}, ...["სერვისი", "მდგომარეობა", "ავტოგაშვება", "PID", "მისამართი", "შემოწმებები", "მოქმედებები"].map((x) => h("th", { scope: "col" }, x)))),
        h("tbody", {}, ...rows.map((r) => h("tr", {},
          h("th", { scope: "row" }, h("a", { href: "#/service/" + r.id }, r.name), h("div", { class: "muted mono" }, r.unit)),
          h("td", {}, badge(r.state), r.detail ? h("div", { class: "muted" }, r.detail) : null, r.since ? h("div", { class: "muted" }, r.since) : null),
          h("td", {}, r.enabled === true ? "✔ დიახ" : r.enabled === false ? "არა" : "-"),
          h("td", { class: "mono" }, r.pid || "-"),
          h("td", { class: "mono" }, ...(r.listen.length ? r.listen.map((x) => h("div", {}, x)) : ["-"])),
          h("td", {}, ...(r.checks.length ? r.checks.map((c) => h("div", {}, c.ok ? "✔ " : "✖ ", `${c.label} `, h("span", { class: "muted mono" }, c.code))) : ["-"])),
          h("td", {}, h("div", { class: "row" }, ...ACTIONS.map(([a, label]) => h("button", { type: "button", class: "btn small", "data-act": a, onclick: () => act(a, [r.id], `${label} — ${r.name}`) }, label))))))))));
    }
    async function load() {
      try { render(await post("/api/v1/ops/status", state.ctx())); }
      catch (e) { errorBox(tableBox, e, load); }
    }
    loading(tableBox, 4);
    await load();
    return { refresh: load };
  },
};
