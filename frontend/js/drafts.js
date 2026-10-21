// Local drafts: unsaved work survives a reload or a closed tab.
// Stored in this browser only (localStorage). If the browser blocks storage, the page works without drafts.

const PREFIX = "cf-draft:";

export function saveDraft(key, state, base = "") {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify({ savedAt: Date.now(), base, state }));
    return true;
  } catch {
    return false;
  }
}

export function loadDraft(key) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return null;
    const draft = JSON.parse(raw);
    return draft && typeof draft === "object" && draft.state ? draft : null;
  } catch {
    return null;
  }
}

export function clearDraft(key) {
  try {
    localStorage.removeItem(PREFIX + key);
  } catch {
    /* nothing to clean up */
  }
}

export function draftTime(draft) {
  return new Date(draft.savedAt).toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export function debounce(fn, ms = 400) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}
