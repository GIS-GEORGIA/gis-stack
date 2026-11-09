// Operations history: deployments, cleanups, backups, restores, rotations... straight from the operation journal.
import { get, post, runJob } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, pill, modal, typedConfirm, toast } from "../ui.js";
import * as state from "../state.js";
import { dateTime, duration } from "../format.js";

export default {
  title: () => "ოპერაციები",
  async mount(root) {
    let filter = "all";
    const box = h("div", { class: "card pad0" });
    const dep = h("div", { class: "card pad0" });
    const out = h("pre", { class: "log", role: "log", "aria-label": "ლოგი", tabindex: "0", hidden: true });
    let admin = false, running = false;
    const tabs = h("div", { class: "tabs", role: "group", "aria-label": "ფილტრი" }, ...[["all", "ყველა"], ["error", "შეცდომები"], ["running", "მიმდინარე"]].map(([k, l]) => h("button", { type: "button", "aria-pressed": String(k === filter), onclick: (e) => { filter = k; tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); load(); } }, l)));
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "ოპერაციები"), h("p", { class: "muted" }, "ინსტალაცია, გასუფთავება, ბექაფი, აღდგენა, პაროლების შეცვლა — ჟურნალიდან")), h("div", { class: "actions" }, tabs, h("a", { class: "btn primary", href: "/" }, "ახალი ინსტალაცია / განახლება (კლასიკური გვერდი)"))),
      h("h2", {}, "განთავსებები და უკან დაბრუნება"), h("p", { class: "muted" }, "ყოველი განთავსების შემდეგ მოწმდება, მუშაობს თუ არა პროგრამები. უკან დაბრუნება ხელახლა იყენებს წინა ჯანმრთელი განთავსების პარამეტრებს; მონაცემებსა და ბექაფებს არ შლის."), dep, out, h("h2", {}, "ყველა ოპერაცია"), box]);

    const OUT = { ok: ["ok", "✔ ჯანმრთელია"], unhealthy: ["bad", "✖ არაჯანმრთელი"], unverified: ["warn", "არ შემოწმდა"], failed: ["bad", "✖ ჩავარდა"], aborted: ["warn", "შეწყდა"], running: ["info", "◌ მიმდინარეობს"], incomplete: ["warn", "დაუსრულებელი"] };
    async function loadDeployments() {
      try {
        const rows = await get("/api/v1/deployments/history", { project: state.getConfig().project, limit: 20 });
        done(dep); clear(dep);
        if (!rows.length) return empty(dep, "განთავსება ამ პროექტისთვის ჯერ არ ჩაწერილა");
        append(dep, h("div", { class: "table-wrap" }, h("table", {}, h("caption", { class: "sr-only" }, "განთავსებების ისტორია"),
          h("thead", {}, h("tr", {}, ...["დრო", "სახეობა", "სამიზნე", "მანქანა", "შედეგი", "პროგრამები", ""].map((x) => h("th", { scope: "col" }, x)))),
          h("tbody", {}, ...rows.map((r, i) => h("tr", {}, h("th", { scope: "row", class: "nowrap" }, dateTime(r.ts)), h("td", {}, r.kind === "rollback" ? "უკან დაბრუნება" : "განთავსება"), h("td", {}, r.target), h("td", { class: "mono" }, r.host),
            h("td", {}, pill(...(OUT[r.outcome] || ["gray", r.outcome])), r.error ? h("details", {}, h("summary", {}, "შეცდომა"), h("span", { class: "mono" }, r.error)) : null),
            h("td", {}, Array.isArray(r.after) ? r.after.map((x) => x.id + (x.ok ? " ✔" : " ✖")).join(", ") : "-"),
            h("td", {}, admin && i === 0 && r.outcome !== "running" ? h("button", { type: "button", class: "btn", disabled: running, onclick: rollback }, "უკან დაბრუნება…") : null)))))));
      } catch (e) { errorBox(dep, e, loadDeployments); }
    }
    async function rollback() {
      let plan;
      try { plan = await post("/api/v1/deployments/rollback/plan", state.ctx()); } catch (e) { toast(e.message, "bad"); return; }
      if (!plan.eligible) { await modal({ title: "უკან დაბრუნება შეუძლებელია", body: h("p", {}, plan.reason), actions: [{ label: "დახურვა", value: true, kind: "primary" }] }); return; }
      const body = h("div", {}, h("p", {}, `ხელახლა გამოიყენება ჯანმრთელი განთავსების პარამეტრები (${dateTime(plan.target.ts)}).`),
        plan.changes.length ? h("p", {}, "შეიცვლება: ", h("span", { class: "mono" }, plan.changes.slice(0, 12).join(", "))) : null,
        h("ul", {}, ...plan.limitations.filter(Boolean).map((x) => h("li", {}, x))));
      if (!(await typedConfirm(body, "ROLLBACK", { ok: "უკან დაბრუნება" }))) return;
      running = true; out.hidden = false; out.textContent = "";
      try {
        const res = await runJob("/api/v1/deployments/rollback", state.ctx({ record: plan.target.id, confirm: true }), { onLines: (l) => { out.textContent += l.join("\n") + "\n"; out.scrollTop = out.scrollHeight; } });
        toast(res.ok ? "უკან დაბრუნება დასრულდა" : "უკან დაბრუნება ვერ დასრულდა — იხილე ლოგი", res.ok ? "ok" : "bad");
      } catch (e) { toast(e.message, "bad"); }
      running = false;
      await Promise.all([loadDeployments(), load()]);
    }

    async function load() {
      try {
        const ops = (await get("/api/v1/journal", { project: state.getConfig().project, limit: 100 })).filter((o) => filter === "all" || (filter === "error" ? o.status === "error" : o.status === "running"));
        done(box); clear(box);
        if (!ops.length) return empty(box, "ოპერაციები ამ ფილტრით არ არის");
        append(box, h("div", { class: "table-wrap" }, h("table", {}, h("caption", { class: "sr-only" }, "ოპერაციების ისტორია"),
          h("thead", {}, h("tr", {}, ...["დრო", "ოპერაცია", "სამიზნე", "მანქანა", "შედეგი", "ხანგრძლივობა", "დეტალები"].map((x) => h("th", { scope: "col" }, x)))),
          h("tbody", {}, ...ops.map((o) => h("tr", {}, h("th", { scope: "row", class: "nowrap" }, dateTime(o.started || o.ended)), h("td", { class: "mono" }, o.op), h("td", {}, o.target), h("td", { class: "mono" }, o.host),
            h("td", {}, o.status === "ok" ? pill("ok", "✔ წარმატება") : o.status === "running" ? pill("info", "◌ მიმდინარეობს") : o.status === "aborted" ? pill("warn", "შეწყდა") : pill("bad", "✖ შეცდომა")),
            h("td", {}, duration(o.seconds)), h("td", {}, o.error ? h("details", {}, h("summary", {}, "შეცდომა"), h("span", { class: "mono" }, o.error)) : "-")))))));
      } catch (e) { errorBox(box, e, load); }
    }
    admin = ((await get("/api/v1/meta").catch(() => ({}))).principal || {}).scope === "admin";
    loading(box, 4); loading(dep, 3);
    await Promise.all([load(), loadDeployments()]);
    return { refresh: () => Promise.all([load(), loadDeployments()]) };
  },
};
