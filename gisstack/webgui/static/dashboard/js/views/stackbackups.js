// Backups of a Compose / Podman / Kubernetes stack: list with integrity status, details, verify, create (optionally encrypted), restore (validate ->
// typed confirmation -> job log), export, delete, retention preview, and recovery of an interrupted restore. Same endpoints as the CLI (/api/v1/stack-backups/*).
import { post, postFile, runJob } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, pill, table, toast, modal, typedConfirm, confirmBox, kv } from "../ui.js";
import * as state from "../state.js";
import { bytes, dateTime } from "../format.js";
import { t } from "../i18n.js";

const NAMES = { postgres: "PostgreSQL / PostGIS", geoserver: "GeoServer-ის მონაცემები", caddy: "Caddy (სერტიფიკატები)", qgis: "QGIS პროექტები", mapproxy_config: "MapProxy-ის კონფიგურაცია", mapproxy_cache: "MapProxy ქეში (დიდი შეიძლება იყოს)" };
const STATUS = { ok: ["ok", "✔ გადამოწმებულია"], partial: ["warn", "დაუსრულებელი"], failed: ["bad", "✖ ჩავარდა"], damaged: ["bad", "✖ დაზიანებულია"] };

export default {
  title: () => "ბექაფები",
  async mount(root) {
    const listBox = h("div", { class: "card pad0" });
    const banner = h("div", {});
    const log = h("pre", { class: "log", hidden: true, role: "log", "aria-label": "ოპერაციის ლოგი", tabindex: "0" });
    const actions = h("div", { class: "actions" }, h("button", { type: "button", class: "btn primary", onclick: () => guarded(createFlow) }, "ახალი ბექაფი"),
      h("button", { type: "button", class: "btn", onclick: () => guarded(pruneFlow) }, "ძველების გასუფთავება"));
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "ბექაფები"), h("p", { class: "muted" }, state.describe()),
      h("p", { class: "banner info" }, h("span", { "aria-hidden": "true" }, "🔒"), "ბექაფში პაროლები არ ინახება, მაგრამ GeoServer-ის მონაცემები მის საკუთარ ჰეშებს შეიცავს — დაიცავი საქაღალდე და გამოიყენე დაშიფვრა.")), actions),
      banner, h("div", { class: "stack" }, listBox, log)]);

    let comps = [];
    const ctx = (extra) => state.ctx(extra);
    async function logged(url, body) {
      log.hidden = false; log.textContent = "";
      return runJob(url, body, { onLines: (l) => { log.textContent += l.join("\n") + "\n"; log.scrollTop = log.scrollHeight; } });
    }
    const busy = (b) => root.querySelectorAll("button").forEach((x) => { x.disabled = b; });
    async function guarded(fn) { busy(true); try { await fn(); } catch (e) { toast(e.message, "bad", 9000); } finally { busy(false); await load(); } }

    async function createFlow() {
      const boxes = comps.map((c) => h("label", { class: "check" }, h("input", { type: "checkbox", value: c.id, checked: !c.optional }), t(NAMES[c.id] || c.id)));
      const note = h("input", { type: "text", id: "sb-note", maxlength: 200, autocomplete: "off" });
      const enc = h("input", { type: "text", id: "sb-enc", autocomplete: "off", placeholder: "key ID / e-mail" });
      const res = await modal({ title: "ახალი ბექაფი", body: h("div", {}, h("div", { class: "stack" }, ...boxes),
        h("div", { class: "field" }, h("label", { for: "sb-note" }, "შენიშვნა"), note),
        h("div", { class: "field" }, h("label", { for: "sb-enc" }, "დაშიფვრა საჯარო გასაღებით (არასავალდებულო)"), enc, h("small", { class: "muted" }, "გასაღები არასდროს ინახება ბექაფში. პაროლით დაშიფვრა — მხოლოდ CLI-დან."))),
      actions: [{ label: "გაუქმება", value: false }, { label: "შექმნა", value: true, kind: "primary" }] });
      if (!res) return;
      const sel = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
      if (!sel.length) return toast(t("აირჩიე მინიმუმ ერთი"), "bad");
      const cache = sel.includes("mapproxy_cache");
      const r = await logged("/api/v1/stack-backups/create", ctx({ components: sel.filter((c) => c !== "mapproxy_cache").concat(cache ? ["mapproxy_cache"] : []), with_cache: cache, note: note.value,
        ...(enc.value.trim() ? { encrypt_to: [enc.value.trim()] } : {}) }));
      toast(r.ok ? t("ბექაფი შექმნილია და გადამოწმებულია") : t("ბექაფი ვერ შეიქმნა — იხილე ლოგი"), r.ok ? "ok" : "bad", 9000);
    }

    async function pruneFlow() {
      const keep = h("input", { type: "number", id: "pr-keep", min: 1, value: 14 });
      const res = await modal({ title: "ძველი ბექაფების წაშლა", body: h("div", {}, h("p", {}, "ყველაზე ახალი გადამოწმებული ბექაფი არასდროს იშლება. ჯერ ნახავ რა წაიშლება."),
        h("div", { class: "field" }, h("label", { for: "pr-keep" }, "დარჩეს უახლესი"), keep)), actions: [{ label: "გაუქმება", value: false }, { label: "ნახვა (არაფერი იშლება)", value: true, kind: "primary" }] });
      if (!res) return;
      const body = ctx({ keep: Number(keep.value) });
      const plan = await post("/api/v1/stack-backups/prune", { ...body, dry_run: true });
      const victims = plan.removed.concat(plan.removed_leftovers);
      if (!victims.length) return toast(t("წასაშლელი არაფერია"), "info");
      if (await typedConfirm(`წაიშლება ${victims.length}: ${victims.join(", ")}`, "DELETE", { ok: "წაშლა" })) {
        await post("/api/v1/stack-backups/prune", { ...body, dry_run: false });
        toast(t("გასუფთავდა"), "ok");
      }
    }

    async function verifyFlow(b) {
      const r = await post("/api/v1/stack-backups/verify", ctx({ id: b.id }));
      if (r.ok) toast(`${t("ბექაფი დაზიანებული არ არის ✔")} (${r.checked})`, "ok");
      else await modal({ title: "ბექაფი დაზიანებულია", body: h("ul", {}, ...r.problems.map((p) => h("li", { class: "mono" }, p))), actions: [{ label: "დახურვა", value: true, kind: "primary" }] });
    }

    async function detailsFlow(b) {
      const m = await post("/api/v1/stack-backups/inspect", ctx({ id: b.id }));
      const files = Object.entries(m.components).map(([n, c]) => [t(NAMES[n] || n), c.files.map((f) => `${f.name} (${bytes(f.bytes)})`).join(", "), String(Object.entries(c.meta || {}).filter(([k]) => k !== "path").map(([k, v]) => `${k}=${v}`).join(" "))]);
      await modal({ title: b.id, body: h("div", { class: "stack" }, kv([["სახეობა", m.kind], ["შექმნილია", m.created], ["სამიზნე", m.target], ["gis-stack", m.gisstack_version], ["დაშიფრულია", m.encrypted ? `${m.encryption.method} ${(m.encryption.recipients || []).join(", ")}` : "არა"],
        ["PostgreSQL", (m.versions || {}).postgres], ["შენიშვნა", m.note], ["მთლიანობა", m.integrity.ok ? "✔" : m.integrity.problems.join("; ")]]),
        table(["კომპონენტი", "ფაილები", "დეტალები"], files, { caption: "კომპონენტები" })), actions: [{ label: "დახურვა", value: true, kind: "primary" }] });
    }

    async function restoreFlow(b) {
      const chk = await post("/api/v1/stack-backups/check", ctx({ id: b.id }));
      if (!chk.ok) return modal({ title: "აღდგენა შეუძლებელია — არაფერი შეცვლილა", body: h("ul", {}, ...chk.problems.map((p) => h("li", {}, p))), actions: [{ label: "დახურვა", value: true, kind: "primary" }] });
      const body = h("div", { class: "stack" }, h("p", { class: "banner warn" }, h("span", { "aria-hidden": "true" }, "!"), "ცოცხალი მონაცემები ჩანაცვლდება. ჯერ ავტომატურად შეიქმნება დადასტურებული ბექაფი; ძველი მონაცემები გვერდით დარჩება; წარუმატებლობისას ყველაფერი უკან დაბრუნდება."),
        h("p", {}, "აღდგება: ", h("span", { class: "mono" }, chk.components.join(", "))));
      if (!(await typedConfirm(body, "RESTORE", { ok: "აღდგენა", title: "აღდგენა" }))) return;
      const r = await logged("/api/v1/stack-backups/restore", ctx({ id: b.id, components: chk.components, confirm: true }));
      toast(r.ok ? t("აღდგენა დასრულდა და გადამოწმდა ✔") : t("აღდგენა ვერ მოხერხდა — იხილე ლოგი (ავტომატური უკან დაბრუნება შესრულდა)"), r.ok ? "ok" : "bad", 12000);
    }

    async function exportFlow(b) {
      const { blob, name } = await postFile("/api/v1/stack-backups/export", ctx({ id: b.id }));
      const a = h("a", { href: URL.createObjectURL(blob), download: name });
      document.body.appendChild(a); a.click(); a.remove();
    }

    async function deleteFlow(b) {
      if (await typedConfirm(`ბექაფი ${b.id} სამუდამოდ წაიშლება.`, "DELETE", { ok: "წაშლა" })) await post("/api/v1/stack-backups/delete", ctx({ id: b.id, confirm: true }));
    }

    async function recoverFlow() {
      if (!(await confirmBox("შეწყვეტილი აღდგენის ნარჩენები გასუფთავდება და წინა მონაცემები დაბრუნდება. გაგრძელდეს?", { ok: "აღდგენა" }))) return;
      const r = await logged("/api/v1/stack-backups/recover", ctx());
      toast(r.ok ? t("წინა მონაცემები დაბრუნდა") : t("ვერ დაბრუნდა — იხილე ლოგი"), r.ok ? "ok" : "bad", 9000);
    }

    async function load() {
      try {
        const res = await post("/api/v1/stack-backups/list", ctx());
        comps = res.components;
        clear(banner);
        if (res.restore_pending) banner.appendChild(h("p", { class: "banner warn", role: "alert" }, h("span", { "aria-hidden": "true" }, "!"), "შეწყვეტილი აღდგენა ჩაწერილია — ახალი აღდგენა დაიბლოკა. ", h("button", { type: "button", class: "btn small", onclick: () => guarded(recoverFlow) }, "წინა მონაცემების დაბრუნება")));
        const rows = res.backups;
        done(listBox); clear(listBox);
        if (!rows.length) return empty(listBox, "ბექაფი ჯერ არ არსებობს", h("button", { type: "button", class: "btn primary", onclick: () => guarded(createFlow) }, "ახალი ბექაფი"));
        append(listBox, h("div", { class: "table-wrap" }, h("table", {}, h("caption", { class: "sr-only" }, "ბექაფები"),
          h("thead", {}, h("tr", {}, ...["ID", "სახეობა", "შექმნილია", "ზომა", "შემადგენლობა", "მდგომარეობა", "მოქმედებები"].map((x) => h("th", { scope: "col" }, x)))),
          h("tbody", {}, ...rows.map((b) => h("tr", {},
            h("th", { scope: "row", class: "mono" }, b.id),
            h("td", {}, b.kind === "pre-restore" ? pill("gray", "აღდგენამდე") : b.kind === "scheduled" ? pill("info", "დაგეგმილი") : b.kind ? pill("gray", "ხელით") : "-", b.encrypted ? pill("info", "🔒 დაშიფრული") : null),
            h("td", { class: "nowrap" }, b.created ? dateTime(Date.parse(b.created) / 1000) : "-"), h("td", {}, b.bytes ? bytes(b.bytes) : "-"), h("td", {}, b.components.join(", ") || "-"),
            h("td", {}, pill(...(STATUS[b.status] || ["gray", b.status]))),
            h("td", {}, b.status === "ok" ? h("div", { class: "row" },
              h("button", { type: "button", class: "btn small", onclick: () => guarded(() => detailsFlow(b)) }, "დეტალები"),
              h("button", { type: "button", class: "btn small", onclick: () => guarded(() => verifyFlow(b)) }, "შემოწმება"),
              h("button", { type: "button", class: "btn small", onclick: () => guarded(() => exportFlow(b)) }, "ჩამოტვირთვა"),
              h("button", { type: "button", class: "btn small danger", onclick: () => guarded(() => restoreFlow(b)) }, "აღდგენა"),
              h("button", { type: "button", class: "btn small danger", onclick: () => guarded(() => deleteFlow(b)) }, "წაშლა")) : h("button", { type: "button", class: "btn small danger", onclick: () => guarded(() => deleteFlow(b)) }, "წაშლა"))))))));
      } catch (e) { errorBox(listBox, e, load); }
    }
    loading(listBox, 3);
    await load();
    return { refresh: load };
  },
};
