// Alert rules manager: built-in rules can be tuned, disabled or reset; custom rules can be created, edited and deleted (admin token needed).
// Every change carries the revision read with the list, so a concurrent change by someone else is reported instead of overwritten.
import { get, post, put, request } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, pill, modal, confirmBox, toast, table } from "../ui.js";
import { featureOn } from "../shared.js";
import { dateTimeSec } from "../format.js";

const LEVEL = { warn: ["warn", "გაფრთხილება"], crit: ["bad", "კრიტიკული"] };
const OPS = [">", ">=", "<", "<=", "=="];

export default {
  title: () => "ალერტების წესები",
  async mount(root) {
    if (!featureOn("monitoring")) {
      append(root, [h("h1", {}, "ალერტების წესები"), h("div", { class: "card" }, h("p", {}, "მონიტორინგი გამორთულია."), h("p", { class: "mono" }, "export GISSTACK_FEATURES=dashboard,monitoring"))]);
      return {};
    }
    let data = { revision: "0", modified: null, metrics: [], rules: [] }, canEdit = false, q = "";
    const box = h("div", { class: "card pad0" });
    const info = h("p", { class: "muted" });
    const search = h("input", { type: "search", id: "rl-q", placeholder: "ძებნა…", "aria-label": "ძებნა" });
    search.addEventListener("input", () => { q = search.value.toLowerCase(); paint(); });
    const add = h("button", { type: "button", class: "btn primary", onclick: () => edit(null) }, "ახალი წესი");
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "ალერტების წესები"),
        h("p", { class: "muted" }, "როდის გაიგზავნოს შეტყობინება. ჩაშენებული წესები არ იშლება — მათი გამორთვა შეიძლება. ცვლილება ეხება მონიტორებს, რომლებიც შენახვის შემდეგ ეშვება (გაშვებული მონიტორი თავიდან გაუშვი)."), info),
      h("div", { class: "actions" }, search, add)), box]);

    const cond = (r) => `${r.metric} ${r.op} ${r.threshold} · ${r.for}×`;
    function paint() {
      clear(box); done(box);
      info.textContent = `${data.modified ? "ბოლო ცვლილება: " + dateTimeSec(data.modified) : "ცვლილება ჯერ არ არის — მოქმედებს ნაგულისხმევი წესები"} · რევიზია ${data.revision}`;
      const rows = data.rules.filter((r) => !q || (r.id + " " + r.metric + " " + r.description + " " + r.text).toLowerCase().includes(q));
      if (!rows.length) return empty(box, "წესი ვერ მოიძებნა");
      append(box, table(["წესი", "მეტრიკა და პირობა", "სიმძიმე", "სტატუსი", "აღწერა", ""], rows.map((r) => [
        h("span", {}, h("span", { class: "mono" }, r.id), " ", r.builtin ? pill("gray", "ჩაშენებული") : pill("info", "საკუთარი"), r.builtin && r.modified ? pill("info", "შეცვლილია") : null),
        h("span", { class: "mono" }, cond(r)), pill(...LEVEL[r.level]), r.enabled ? pill("ok", "ჩართულია") : pill("gray", "გამორთულია"),
        r.description || r.text,
        !canEdit ? "" : h("span", { class: "row" }, h("button", { type: "button", class: "btn", onclick: () => edit(r) }, "რედაქტირება"),
          h("button", { type: "button", class: "btn", onclick: () => toggle(r) }, r.enabled ? "გამორთვა" : "ჩართვა"),
          r.builtin ? (r.modified ? h("button", { type: "button", class: "btn", onclick: () => reset(r) }, "ნაგულისხმევი") : null)
            : h("button", { type: "button", class: "btn danger", onclick: () => remove(r) }, "წაშლა")),
      ]), { caption: "ალერტების წესები" }));
    }

    async function apply(fn, okText) {
      try { data = await fn(); paint(); toast(okText, "ok"); return true; }
      catch (e) {
        toast(e.code === "conflict" ? "წესები სხვამ შეცვალა — სია თავიდან ჩაიტვირთა, სცადე ისევ" : e.message, "bad");
        if (e.code === "conflict") await load();
        return false;
      }
    }
    const toggle = (r) => apply(() => put(`/api/v1/monitor/rules/${r.id}`, { revision: data.revision, enabled: !r.enabled }), r.enabled ? "გამორთულია" : "ჩართულია");
    async function reset(r) {
      if (await confirmBox(`წესი ${r.id} დაბრუნდება ნაგულისხმევ მნიშვნელობებზე.`)) await apply(() => put(`/api/v1/monitor/rules/${r.id}`, { revision: data.revision, reset: true }), "დაბრუნდა ნაგულისხმევზე");
    }
    async function remove(r) {
      if (await confirmBox(`წესი ${r.id} სამუდამოდ წაიშლება. გაგრძელდეს?`, { danger: true, ok: "წაშლა" })) await apply(() => request("DELETE", `/api/v1/monitor/rules/${r.id}?revision=${encodeURIComponent(data.revision)}`), "წაიშალა");
    }

    // create (r === null) or edit; the dialog stays "open" after a refused save: it is re-shown with the entered values and the server's reason
    async function edit(r, draft, problem) {
      const b = r && r.builtin;
      const v = draft || (r ? { ...r, renotifyH: r.renotify / 3600 } : { id: "", metric: data.metrics[0] || "host_cpu_percent", op: ">", threshold: 90, for: 3, level: "warn", text: "{subject}: {value:.0f}", description: "", enabled: true, renotifyH: 6 });
      const f = {
        id: h("input", { id: "rl-id", type: "text", value: v.id, autocomplete: "off", disabled: !!r, placeholder: "my_rule" }),
        metric: h("select", { id: "rl-metric", disabled: b }, ...data.metrics.map((m) => h("option", { value: m, selected: m === v.metric }, m))),
        op: h("select", { id: "rl-op", disabled: b }, ...OPS.map((o) => h("option", { value: o, selected: o === v.op }, o))),
        threshold: h("input", { id: "rl-th", type: "number", step: "any", value: String(v.threshold) }),
        for: h("input", { id: "rl-for", type: "number", min: "1", max: "1000", step: "1", value: String(v.for) }),
        level: h("select", { id: "rl-level" }, ...Object.entries(LEVEL).map(([k, [, l]]) => h("option", { value: k, selected: k === v.level }, l))),
        renotifyH: h("input", { id: "rl-rn", type: "number", min: "0", max: "720", step: "any", value: String(v.renotifyH) }),
        text: h("input", { id: "rl-text", type: "text", value: v.text, maxlength: "200", disabled: b }),
        description: h("input", { id: "rl-desc", type: "text", value: v.description || "", maxlength: "200", disabled: b }),
        enabled: h("input", { id: "rl-en", type: "checkbox", checked: v.enabled }),
      };
      const fld = (label, el, hint) => h("div", { class: "field" }, h("label", { for: el.id }, label), el, hint ? h("small", { class: "muted" }, hint) : null);
      const body = h("form", { onsubmit: (e) => e.preventDefault() }, problem ? h("p", { class: "error-box", role: "alert" }, problem) : null,
        fld("სახელი (id)", f.id, r ? null : "a-z, 0-9, _ ; 3–41 სიმბოლო"), fld("მეტრიკა", f.metric), fld("შედარება", f.op), fld("ზღვარი", f.threshold),
        fld("ზედიზედ ნიმუშები", f.for, "ალერტი ამდენი ზედიზედ გადაჭარბების შემდეგ ჩაირთვება"), fld("სიმძიმე", f.level),
        fld("გამეორება (საათი)", f.renotifyH, "0 — შეტყობინება არ მეორდება"), fld("ტექსტი", f.text, "ტექსტში გამოიყენება: {value}, {subject}, ფორმატი {value:.0f}"), fld("აღწერა", f.description),
        h("label", { class: "check", for: "rl-en" }, f.enabled, "ჩართულია"));
      const res = await modal({ title: r ? `წესი: ${r.id}` : "ახალი წესი", body, actions: [{ label: "გაუქმება", value: false }, { label: "შენახვა", value: true, kind: "primary" }] });
      if (!res) return;
      const cur = {
        id: f.id.value.trim(), metric: f.metric.value || v.metric, op: f.op.value || v.op, threshold: Number(f.threshold.value), for: Number(f.for.value), level: f.level.value,
        text: f.text.value || v.text, description: f.description.value, enabled: f.enabled.checked, renotifyH: Number(f.renotifyH.value),
      };
      if (f.threshold.value.trim() === "" || !Number.isFinite(cur.threshold) || !Number.isInteger(cur.for) || cur.for < 1 || cur.for > 1000 || !Number.isFinite(cur.renotifyH) || cur.renotifyH < 0 || cur.renotifyH > 720)
        return edit(r, cur, "ზღვარი უნდა იყოს რიცხვი, ნიმუშების რაოდენობა — მთელი 1…1000, გამეორება — 0…720 საათი");
      const renotify = cur.renotifyH * 3600;
      try {
        if (!r) data = await post("/api/v1/monitor/rules", { revision: data.revision, id: cur.id, metric: cur.metric, op: cur.op, threshold: cur.threshold, for: cur.for, level: cur.level, text: cur.text, description: cur.description, enabled: cur.enabled, renotify });
        else if (b) data = await put(`/api/v1/monitor/rules/${r.id}`, { revision: data.revision, threshold: cur.threshold, for: cur.for, level: cur.level, enabled: cur.enabled, renotify });
        else data = await put(`/api/v1/monitor/rules/${r.id}`, { revision: data.revision, metric: cur.metric, op: cur.op, threshold: cur.threshold, for: cur.for, level: cur.level, text: cur.text, description: cur.description, enabled: cur.enabled, renotify });
        paint(); toast("შენახულია", "ok");
      } catch (e) {
        if (e.code === "conflict" && r) { await load(); toast("წესები სხვამ შეცვალა — სია თავიდან ჩაიტვირთა, სცადე ისევ", "bad"); return; }
        return edit(r, cur, e.message);
      }
    }

    async function load() {
      loading(box, 4);
      try { data = await get("/api/v1/monitor/rules"); paint(); } catch (e) { errorBox(box, e, load); }
    }
    canEdit = ((await get("/api/v1/meta").catch(() => ({}))).principal || {}).scope === "admin";
    add.hidden = !canEdit;
    await load();
    return { refresh: load };
  },
};
