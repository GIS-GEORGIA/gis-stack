// Operations history: deployments, cleanups, backups, restores, rotations... straight from the operation journal.
import { get } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, pill } from "../ui.js";
import * as state from "../state.js";
import { dateTime, duration } from "../format.js";

export default {
  title: () => "ოპერაციები",
  async mount(root) {
    let filter = "all";
    const box = h("div", { class: "card pad0" });
    const tabs = h("div", { class: "tabs", role: "group", "aria-label": "ფილტრი" }, ...[["all", "ყველა"], ["error", "შეცდომები"], ["running", "მიმდინარე"]].map(([k, l]) => h("button", { type: "button", "aria-pressed": String(k === filter), onclick: (e) => { filter = k; tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); load(); } }, l)));
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "ოპერაციები"), h("p", { class: "muted" }, "ინსტალაცია, გასუფთავება, ბექაფი, აღდგენა, პაროლების შეცვლა — ჟურნალიდან")), h("div", { class: "actions" }, tabs, h("a", { class: "btn primary", href: "/" }, "ახალი ინსტალაცია / განახლება (კლასიკური გვერდი)"))), box]);

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
    loading(box, 4);
    await load();
    return { refresh: load };
  },
};
