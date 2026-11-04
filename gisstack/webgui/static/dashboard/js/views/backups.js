// Backups: list, create, verify, prune and a restore wizard (plan -> explicit typed confirmation -> run). Everything goes through the same
// endpoints and the same server-side tool as the CLI (/api/backup/*, gisstack-backup), so the safety rules cannot differ.
import { post, runJob } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, pill, table, toast, modal, typedConfirm } from "../ui.js";
import * as state from "../state.js";
import { bytes, dateTime } from "../format.js";
import { t } from "../i18n.js";

const COMPS = [["config", "კონფიგურაცია"], ["postgres", "PostgreSQL"], ["geoserver", "GeoServer-ის მონაცემები"], ["qgis", "QGIS პროექტები"], ["cache", "MapProxy ქეში (დიდი შეიძლება იყოს)"]];

export default {
  title: () => "ბექაფები",
  async mount(root) {
    if (state.getConfig().deploy_target !== "native") {
      append(root, [h("h1", {}, "ბექაფები"), h("div", { class: "card" }, h("p", {}, "ბექაფი და აღდგენა ჯერ მხოლოდ native სამიზნეზეა."), h("p", { class: "muted" }, `მიმდინარე სამიზნე: ${state.getConfig().deploy_target}. შეცვალე „კავშირი“-ში.`))]);
      return {};
    }
    const listBox = h("div", { class: "card pad0" });
    const log = h("pre", { class: "log", hidden: true, "aria-label": "ოპერაციის ლოგი" });
    const actions = h("div", { class: "actions" },
      h("button", { type: "button", class: "btn primary", onclick: createFlow }, "ახალი ბექაფი"),
      h("button", { type: "button", class: "btn", onclick: pruneFlow }, "ძველების გასუფთავება"));
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "ბექაფები"), h("p", { class: "muted" }, state.describe()),
      h("p", { class: "banner info" }, h("span", { "aria-hidden": "true" }, "🔒"), "ბექაფი პაროლებს შეიცავს (credentials, როლების ჰეშები) — დაიცავი საქაღალდე და ჩამოტვირთული ასლები.")), actions), h("div", { class: "stack" }, listBox, log)]);

    async function logged(url, body) {
      log.hidden = false; log.textContent = "";
      const r = await runJob(url, body, { onLines: (l) => { log.textContent += l.join("\n") + "\n"; log.scrollTop = log.scrollHeight; } });
      return r;
    }
    const busy = (b) => root.querySelectorAll("button").forEach((x) => { x.disabled = b; });
    async function guarded(fn) { busy(true); try { await fn(); } catch (e) { toast(e.message, "bad"); } finally { busy(false); await load(); } }

    function createFlow() {
      return guarded(async () => {
        const boxes = COMPS.map(([id, l]) => h("label", { class: "check" }, h("input", { type: "checkbox", value: id, checked: id !== "cache" }), t(l)));
        const note = h("input", { type: "text", id: "bk-note", maxlength: 200, autocomplete: "off" });
        const res = await modal({ title: "ახალი ბექაფი", body: h("div", {}, h("div", { class: "stack" }, ...boxes), h("div", { class: "field", style: { marginTop: ".8rem" } }, h("label", { for: "bk-note" }, "შენიშვნა"), note)),
          actions: [{ label: "გაუქმება", value: false }, { label: "შექმნა", value: true, kind: "primary" }] });
        if (!res) return;
        const comps = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
        if (!comps.length) return toast(t("აირჩიე მინიმუმ ერთი"), "bad");
        const r = await logged("/api/backup/create", state.ctx({ components: comps.filter((c) => c !== "cache"), with_cache: comps.includes("cache"), note: note.value }));
        toast(r.ok ? t("ბექაფი შექმნილია და გადამოწმებულია") : t("ბექაფი ვერ შეიქმნა"), r.ok ? "ok" : "bad");
      });
    }

    function pruneFlow() {
      return guarded(async () => {
        const keep = h("input", { type: "number", id: "pr-keep", min: 1, value: 14 }), days = h("input", { type: "number", id: "pr-days", min: 0, placeholder: "—" });
        const res = await modal({ title: "ძველი ბექაფების წაშლა", body: h("div", {}, h("p", {}, "ყველაზე ახალი ბექაფი არასდროს იშლება. ჯერ ნახავ რა წაიშლება."),
          h("div", { class: "field" }, h("label", { for: "pr-keep" }, "დარჩეს უახლესი"), keep), h("div", { class: "field" }, h("label", { for: "pr-days" }, "წაიშალოს მხოლოდ ამდენ დღეზე ძველი (არასავალდებულო)"), days)),
          actions: [{ label: "გაუქმება", value: false }, { label: "ნახვა (არაფერი იშლება)", value: true, kind: "primary" }] });
        if (!res) return;
        const body = state.ctx({ keep: Number(keep.value), days: days.value === "" ? null : Number(days.value) });
        const plan = await logged("/api/backup/prune", { ...body, dry_run: true });
        const victims = plan.lines.filter((l) => l.startsWith("WOULD_DELETE=")).map((l) => l.slice(13));
        if (!plan.ok || !victims.length) return toast(plan.ok ? t("წასაშლელი არაფერია") : t("ვერ შესრულდა"), plan.ok ? "info" : "bad");
        if (await typedConfirm(`წაიშლება ${victims.length} ბექაფი: ${victims.join(", ")}`, "DELETE", { ok: "წაშლა" })) {
          const r = await logged("/api/backup/prune", { ...body, dry_run: false });
          toast(r.ok ? t("გასუფთავდა") : t("ვერ შესრულდა"), r.ok ? "ok" : "bad");
        }
      });
    }

    function verifyFlow(id) {
      return guarded(async () => { const r = await logged("/api/backup/verify", state.ctx({ id })); toast(r.ok ? t("ბექაფი დაზიანებული არ არის ✔") : t("ბექაფი დაზიანებულია!"), r.ok ? "ok" : "bad"); });
    }

    function restoreFlow(b, staging) {
      return guarded(async () => {
        const only = b.components;
        const plan = await logged("/api/backup/restore", state.ctx({ id: b.id, only, dry_run: true }));
        if (!plan.ok) return toast(t("გეგმა ვერ დაიგეგმა — ბექაფი დაზიანებულია ან მოთხოვნები არ სრულდება"), "bad");
        const text = plan.lines.filter((l) => l.startsWith("RESTORE_PLAN") || /^\s{2}\w/.test(l)).join("\n");
        const intro = staging
          ? h("p", {}, "ბექაფი გაიშლება ცოცხალი მონაცემების გვერდით (ბაზა — ახალი სახელით). არაფერი ჩანაცვლდება.")
          : h("div", {}, h("p", { class: "banner warn" }, h("span", { "aria-hidden": "true" }, "!"), "ცოცხალი მონაცემები ჩანაცვლდება. ჯერ ავტომატურად შეიქმნება დადასტურებული ბექაფი; ძველი მონაცემები გვერდით დარჩება; წარუმატებლობისას ყველაფერი უკან დაბრუნდება."));
        const proceed = staging ? await modal({ title: "გაშლა (staging)", body: h("div", {}, intro, h("pre", { class: "log" }, text)), actions: [{ label: "გაუქმება", value: false }, { label: "გაშლა", value: true, kind: "primary" }] })
          : await typedConfirm(h("div", {}, intro, h("pre", { class: "log" }, text)), "RESTORE", { ok: "აღდგენა", title: "აღდგენა" });
        if (!proceed) return;
        const r = await logged("/api/backup/restore", state.ctx({ id: b.id, only, dry_run: false, staging, confirm: true }));
        toast(r.ok ? t(staging ? "გაიშალა" : "აღდგენა დასრულდა და გადამოწმდა ✔") : t("აღდგენა ვერ მოხერხდა — იხილე ლოგი (ავტომატური უკან დაბრუნება შესრულდა)"), r.ok ? "ok" : "bad", 9000);
      });
    }

    async function load() {
      try {
        const rows = (await post("/api/backup/list", state.ctx())).backups;
        done(listBox); clear(listBox);
        if (!rows.length) return empty(listBox, "ბექაფი ჯერ არ არსებობს", h("button", { type: "button", class: "btn primary", onclick: createFlow }, "ახალი ბექაფი"));
        append(listBox, h("div", { class: "table-wrap" }, h("table", {}, h("caption", { class: "sr-only" }, "ბექაფები"),
          h("thead", {}, h("tr", {}, ...["ID", "სახეობა", "შექმნილია", "ზომა", "შემადგენლობა", "მდგომარეობა", "მოქმედებები"].map((x) => h("th", { scope: "col" }, x)))),
          h("tbody", {}, ...rows.map((b) => h("tr", {},
            h("th", { scope: "row", class: "mono" }, b.id),
            h("td", {}, b.kind === "pre-restore" ? pill("gray", "აღდგენამდე") : b.kind === "scheduled" ? pill("info", "დაგეგმილი") : pill("gray", "ხელით")),
            h("td", { class: "nowrap" }, b.created === "-" ? "-" : dateTime(Date.parse(b.created) / 1000)), h("td", {}, bytes(b.bytes)), h("td", {}, b.components.join(", ")),
            h("td", {}, b.status === "complete" ? pill("ok", "✔ სრულია") : pill("bad", "✖ " + b.status)),
            h("td", {}, b.status === "complete" ? h("div", { class: "row" },
              h("button", { type: "button", class: "btn small", onclick: () => verifyFlow(b.id) }, "შემოწმება"),
              h("button", { type: "button", class: "btn small", onclick: () => restoreFlow(b, true) }, "გაშლა"),
              h("button", { type: "button", class: "btn small danger", onclick: () => restoreFlow(b, false) }, "აღდგენა")) : "-")))))));
      } catch (e) { errorBox(listBox, e, load); }
    }
    loading(listBox, 3);
    await load();
    return { refresh: load };
  },
};
