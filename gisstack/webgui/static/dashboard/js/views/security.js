// Security: owner-only permission checks of this machine's gis-stack data and the administrative audit trail (admin token needed).
import { get } from "../api.js";
import { h, append, clear, loading, done, empty, errorBox, pill, table } from "../ui.js";
import { dateTimeSec } from "../format.js";

const LABELS = {
  home: "gis-stack-ის მონაცემების საქაღალდე", token: "ვებ წვდომის ტოკენი", api_tokens: "API ტოკენები", audit: "აუდიტის ჟურნალი",
  servers: "სერვერების პროფილები", known_hosts: "დაფიქსირებული SSH გასაღებები", secrets_dir: "გენერირებული პაროლების საქაღალდე",
  secret_files: "გენერირებული პაროლების ფაილები", signature: "განახლების ხელმოწერის წესი", token_env: "წვდომის ტოკენის წყარო",
};
const KIND = { ok: "ok", warn: "warn", fail: "bad", info: "info" };
const TEXT = { ok: "✔ წესრიგშია", warn: "⚠ გაფრთხილება", fail: "✖ პრობლემა", info: "ℹ ინფორმაცია" };
const FIELDS = ["principal", "scope", "project", "host", "server", "phase", "ok", "error"];

export default {
  title: () => "უსაფრთხოება",
  async mount(root) {
    let prefix = "";
    const posture = h("div", { class: "card pad0" });
    const audit = h("div", { class: "card pad0" });
    const filter = h("select", { id: "sec-filter", "aria-label": "მოვლენის ტიპი" }, ...[["", "ყველა"], ["postgis.", "PostGIS"], ["servers.", "სერვერები"], ["backup", "ბექაფები"]]
      .map(([v, l]) => h("option", { value: v }, l)));
    filter.addEventListener("change", () => { prefix = filter.value; loadAudit(); });
    append(root, [h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "უსაფრთხოება"),
        h("p", { class: "muted" }, "ამ კომპიუტერზე (სადაც gis-stack მუშაობს) ფაილების უფლებები და ადმინისტრაციული მოქმედებების ჟურნალი. საიდუმლო მნიშვნელობები არ ჩანს."))),
      h("h2", {}, "უფლებები და წესები"), posture,
      h("div", { class: "page-head" }, h("h2", {}, "აუდიტის ჟურნალი"), h("div", { class: "actions" }, h("label", { for: "sec-filter" }, "ფილტრი"), filter)), audit]);

    async function loadPosture() {
      try {
        const rows = await get("/api/v1/security/posture");
        done(posture); clear(posture);
        append(posture, table(["შემოწმება", "შედეგი", "დეტალი"], rows.map((r) => [LABELS[r.id] || r.label, pill(KIND[r.status] || "info", TEXT[r.status] || r.status), h("span", { class: "mono" }, r.detail)])));
      } catch (e) { errorBox(posture, e, loadPosture); }
    }
    async function loadAudit() {
      try {
        const rows = await get("/api/v1/security/audit", { limit: 100, ...(prefix ? { event: prefix } : {}) });
        done(audit); clear(audit);
        if (!rows.length) return empty(audit, "აუდიტის ჟურნალში ჩანაწერები ჯერ არ არის");
        append(audit, table(["დრო", "მოვლენა", "დეტალები"], rows.map((e) => [dateTimeSec(e.ts), h("span", { class: "mono" }, e.event || "-"),
          h("span", { class: "mono" }, FIELDS.filter((k) => e[k] !== undefined && e[k] !== "").map((k) => `${k}=${e[k]}`).join("  ") || "-")]),
          { caption: "აუდიტის ჟურნალი" }));
      } catch (e) { errorBox(audit, e, loadAudit); }
    }
    loading(posture, 3); loading(audit, 3);
    await Promise.all([loadPosture(), loadAudit()]);
    return { refresh: () => Promise.all([loadPosture(), loadAudit()]) };
  },
};
