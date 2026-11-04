// Settings: language, theme, the state of the feature flags and facts about this installation.
import { h, append, kv, pill, table } from "../ui.js";
import * as state from "../state.js";
import { app, featureOn } from "../shared.js";
import { saveSettings, connectDialog, applySettings } from "../app.js";

const FEATURES = [
  ["dashboard", "ეს დაფა"], ["monitoring", "მონიტორინგი: მეტრიკები, ისტორია, ალერტები"], ["prometheus", "Prometheus ტექსტური ექსპორტი (/metrics)"],
  ["backups", "ბექაფების ინტერფეისი (დაგეგმილი)"], ["geoserver_manager", "GeoServer მენეჯერი (დაგეგმილი)"], ["postgis_manager", "PostGIS მენეჯერი (დაგეგმილი)"],
  ["mapproxy_manager", "MapProxy მენეჯერი (დაგეგმილი)"], ["multi_server", "მრავალი სერვერი (დაგეგმილი)"], ["api", "ვერსიული API (დაგეგმილი)"], ["plugins", "პლაგინები (დაგეგმილი)"],
];

export default {
  title: () => "პარამეტრები",
  async mount(root) {
    const lang = h("select", { id: "st-lang", onchange: (e) => saveSettings({ lang: e.target.value }) }, ...[["auto", "ავტომატური"], ["ka", "ქართული"], ["en", "English"]].map(([v, l]) => h("option", { value: v, selected: v === app.settings.lang }, l)));
    const theme = h("select", { id: "st-theme", onchange: (e) => saveSettings({ theme: e.target.value }) }, ...[["auto", "სისტემის"], ["light", "ღია"], ["dark", "მუქი"]].map(([v, l]) => h("option", { value: v, selected: v === app.settings.theme }, l)));
    append(root, [h("div", { class: "page-head" }, h("h1", {}, "პარამეტრები")), h("div", { class: "grid cols-2" },
      h("section", { class: "card", "aria-labelledby": "st-ui" }, h("h2", { id: "st-ui" }, "გარეგნობა"),
        h("div", { class: "field" }, h("label", { for: "st-lang" }, "ენა"), lang), h("div", { class: "field" }, h("label", { for: "st-theme" }, "თემა"), theme),
        h("small", { class: "muted" }, "იგივე პარამეტრებია, რაც კლასიკურ გვერდზე და დესკტოპ პროგრამაში (~/.gisstack/settings.json).")),
      h("section", { class: "card", "aria-labelledby": "st-conn" }, h("h2", { id: "st-conn" }, "კავშირი"),
        kv([["პროექტი", state.getConn().project || "(ნაგულისხმევი)"], ["სამიზნე", state.getConfig().deploy_target], ["სად", state.getConn().mode === "ssh" ? `SSH: ${state.getConn().ssh.user}@${state.getConn().ssh.host}:${state.getConn().ssh.port}` : "ეს კომპიუტერი"], ["ვერსია", app.version]]),
        h("p", {}, h("button", { type: "button", class: "btn", onclick: () => connectDialog() }, "კავშირის შეცვლა"))),
      h("section", { class: "card pad0", style: { gridColumn: "1 / -1" }, "aria-labelledby": "st-ft" }, h("h2", { id: "st-ft", style: { padding: ".8rem 1rem 0" } }, "ფუნქციები (feature flags)"),
        table(["ფუნქცია", "აღწერა", "მდგომარეობა"], FEATURES.map(([k, d]) => [h("span", { class: "mono" }, k), d, featureOn(k) ? pill("ok", "✔ ჩართულია") : pill("gray", "გამორთულია")])),
        h("p", { class: "muted", style: { padding: "0 1rem 1rem" } }, "ჩასართავად გაუშვი ვებ სერვერი ცვლადით: ", h("span", { class: "mono" }, "GISSTACK_FEATURES=dashboard,monitoring"), " (ან all)."))),
    ]);
    return {};
  },
};
