// Number / time formatting - pure functions (unit-tested with node).
export function bytes(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "-";
  n = Number(n);
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? String(Math.round(n)) : n.toFixed(n >= 100 ? 0 : 1)) + " " + u[i];
}
export function pct(v, digits = 0) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(digits) + " %";
}
export function duration(sec) {
  if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return "-";
  sec = Math.max(0, Math.round(Number(sec)));
  if (sec < 60) return sec + " წმ";
  if (sec < 3600) return Math.floor(sec / 60) + " წთ";
  if (sec < 86400) return Math.floor(sec / 3600) + " სთ " + Math.floor((sec % 3600) / 60) + " წთ";
  return Math.floor(sec / 86400) + " დღე " + Math.floor((sec % 86400) / 3600) + " სთ";
}
export function ago(ts, now = Date.now() / 1000) {
  if (ts === null || ts === undefined) return "-";
  const d = now - Number(ts);
  return d < 5 ? "ახლახან" : duration(d) + " წინ";
}
const pad = (n) => String(n).padStart(2, "0");
export function clock(ts) {
  if (ts === null || ts === undefined) return "-";
  const d = new Date(Number(ts) * 1000);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}
export function dateTime(ts) {
  if (ts === null || ts === undefined) return "-";
  const d = new Date(Number(ts) * 1000);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
export function dateTimeSec(ts) {
  if (ts === null || ts === undefined) return "-";
  return dateTime(ts) + ":" + clock(ts).slice(6);
}
export function seconds(v) { return v === null || v === undefined ? "-" : Number(v).toFixed(Number(v) < 1 ? 3 : 1) + " s"; }
