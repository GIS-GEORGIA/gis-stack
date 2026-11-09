// The "connection": which project (configuration), which machine (this one / SSH) and which deployment target the dashboard talks about.
// Kept in sessionStorage only - an SSH key *path* is never written to disk by the browser, and closing the tab forgets it.
import { get } from "./api.js";

const KEY = "gs_dash_conn";
const DEFAULT = { project: "", mode: "local", target: "", server: "", ssh: { host: "", port: 22, user: "root", key: "" } };
let conn = load();
let config = null;
const listeners = new Set();

function load() {
  try { return Object.assign({}, DEFAULT, JSON.parse(sessionStorage.getItem(KEY) || "{}")); } catch (e) { return { ...DEFAULT }; }
}
export const getConn = () => conn;
export const getConfig = () => config;
export function onConn(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export async function setConn(next) {
  conn = Object.assign({}, DEFAULT, next, { ssh: Object.assign({}, DEFAULT.ssh, next.ssh || {}) });
  try { sessionStorage.setItem(KEY, JSON.stringify(conn)); } catch (e) { /* private mode: keep it in memory */ }
  config = null;
  await loadConfig();
  listeners.forEach((fn) => fn(conn));
}
export async function loadConfig() {
  if (config) return config;
  if (conn.project) config = (await get("/api/v1/projects/" + encodeURIComponent(conn.project))).config;
  else config = (await get("/api/v1/catalog")).defaults;
  if (conn.target) config.deploy_target = conn.target;
  return config;
}
// what every request about the machine needs
export function ctx(extra) {
  // a saved server profile brings its own credentials and pinned host key: only its id travels
  if (conn.mode === "ssh" && conn.server) return Object.assign({ config, mode: "ssh", server: conn.server }, extra || {});
  return Object.assign({ config, mode: conn.mode, ssh: conn.mode === "ssh" ? conn.ssh : {} }, extra || {});
}
export function hostLabel() { return conn.mode === "ssh" ? conn.ssh.host : "localhost"; }
export function describe() {
  if (!config) return "";
  return `${config.project || "gisstack"} · ${config.deploy_target} · ${conn.mode === "ssh" ? `${conn.server ? conn.server + " · " : ""}${conn.ssh.user}@${conn.ssh.host}` : "ეს კომპიუტერი"}`;
}
export function query(extra) { return Object.assign({ project: (config && config.project) || "gisstack", host: hostLabel() }, extra || {}); }
