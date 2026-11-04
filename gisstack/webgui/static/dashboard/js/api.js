// fetch wrapper: JSON in/out, the CSRF header on every state-changing call, one error type for the old {error: "..."} and the new envelope shapes.
export class ApiError extends Error {
  constructor(message, code, status, payload) { super(message); this.code = code || null; this.status = status || 0; this.payload = payload; }
}

export async function request(method, url, body) {
  let r;
  try {
    r = await fetch(url, {
      method, credentials: "same-origin",
      headers: method === "GET" ? {} : { "Content-Type": "application/json", "X-GisStack": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError("სერვერთან კავშირი ვერ დამყარდა", "network", 0);
  }
  let j = null;
  try { j = await r.json(); } catch (e) { /* not JSON (e.g. 401 text) */ }
  if (r.status === 401) throw new ApiError("სესია დასრულდა — გახსენი `gisstack web`-ის მიერ დაბეჭდილი ბმული თავიდან", "unauthorized", 401);
  if (!r.ok) {
    const msg = j && j.error ? (typeof j.error === "string" ? j.error : j.error.message) : r.statusText;
    const code = j && j.error && typeof j.error === "object" ? j.error.code : (j && j.code) || null;
    throw new ApiError(msg || "შეცდომა", code, r.status, j);
  }
  if (j && typeof j === "object" && "data" in j && "meta" in j) return j.data;   // /api/v1 envelope
  return j;
}
export const get = (url, params) => request("GET", params ? url + "?" + new URLSearchParams(params) : url);
export const post = (url, body) => request("POST", url, body || {});

// Start a background job (deploy, backup, logs ...) and follow its log. onLines(newLines) is called as they arrive.
export async function runJob(url, body, { onLines, signal, interval = 900 } = {}) {
  const { job } = await post(url, body);
  let next = 0;
  const lines = [];
  for (;;) {
    if (signal && signal.aborted) { try { await post(`/api/jobs/${job}/cancel`); } catch (e) { /* already gone */ } return { ok: false, lines, cancelled: true, job }; }
    const j = await get(`/api/jobs/${job}`, { since: next });
    if (j.lines.length) { lines.push(...j.lines); if (onLines) onLines(j.lines); }
    next = j.next;
    if (j.done) return { ok: j.ok, lines, job };
    await new Promise((res) => setTimeout(res, interval));
  }
}
