// State shared by app.js and the views (kept apart so the views never import the entry module).
export const app = { features: [], version: "", settings: { lang: "auto", theme: "auto" }, projects: [] };
export function featureOn(name) { return app.features.includes(name); }
export function setDot(kind) { const d = document.getElementById("conn-dot"); if (d) d.className = "dot " + kind; }
