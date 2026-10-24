// Drafts: unsaved work is saved continuously in the program's data folder (server side), so it survives
// closing the window, a restart of the computer or an update. localStorage is only a fast local mirror
// used when the server cannot be reached for a moment.

const PREFIX = "cf-draft:";

function mirror(key, value) {
  try {
    if (value === null) localStorage.removeItem(PREFIX + key);
    else localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch { /* storage blocked: the server copy is enough */ }
}

function mirrored(key) {
  try {
    const d = JSON.parse(localStorage.getItem(PREFIX + key) || "null");
    return d && typeof d === "object" && d.state ? d : null;
  } catch { return null; }
}

const listeners = new Set();
// UI hook: fn({ key, state: "saving" | "saved" | "error", at })
export function onDraftStatus(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function notify(s) { listeners.forEach((fn) => { try { fn(s); } catch { /* ignore */ } }); }

export async function saveDraft(key, state, base = "") {
  const draft = { savedAt: Date.now(), base, state };
  mirror(key, draft);
  notify({ key, state: "saving" });
  try {
    const r = await fetch(`/api/drafts/${encodeURIComponent(key)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, keepalive: true,
      body: JSON.stringify({ state, base }),
    });
    if (!r.ok) throw new Error(String(r.status));
    notify({ key, state: "saved", at: Date.now() });
    return true;
  } catch {
    notify({ key, state: "error" });
    return false;
  }
}

export async function loadDraft(key) {
  const local = mirrored(key);
  try {
    const r = await fetch(`/api/drafts/${encodeURIComponent(key)}`);
    if (!r.ok) throw new Error(String(r.status));
    const { draft } = await r.json();
    // A newer local copy means the last server write did not arrive (the app was closed mid-save).
    if (local && (!draft || local.savedAt > draft.savedAt + 1000)) return local;
    return draft && draft.state ? draft : null;
  } catch {
    return local;
  }
}

export function clearDraft(key) {
  mirror(key, null);
  return fetch(`/api/drafts/${encodeURIComponent(key)}`, { method: "DELETE", keepalive: true }).catch(() => {});
}

export function draftTime(draft) {
  return new Date(draft.savedAt).toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export function debounce(fn, ms = 400) {
  let timer = null;
  let lastArgs = null;
  const d = (...args) => {
    lastArgs = args;
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...lastArgs); }, ms);
  };
  // Run a pending call now (e.g. when the window is closing).
  d.flush = () => { if (timer) { clearTimeout(timer); timer = null; fn(...lastArgs); } };
  // Drop a pending call (e.g. the draft was just discarded: it must not be written again).
  d.cancel = () => { clearTimeout(timer); timer = null; };
  return d;
}
