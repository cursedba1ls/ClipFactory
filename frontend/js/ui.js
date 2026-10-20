// Shared UI helpers: escaping, formatting, icons, toasts, modals, status badges, job panel, timeline.

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return "—";
  sec = Math.max(0, sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

export function fmtSecondsShort(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `${(Math.round(sec * 10) / 10).toString().replace(".", ",")} с`;
  return fmtDuration(sec);
}

export function fmtEta(sec) {
  if (sec == null || !isFinite(sec)) return null;
  if (sec < 60) return `≈ ${Math.max(1, Math.round(sec))} с`;
  if (sec < 3600) return `≈ ${Math.round(sec / 60)} мин`;
  return `≈ ${Math.floor(sec / 3600)} ч ${Math.round((sec % 3600) / 60)} мин`;
}

export function fmtSize(bytes) {
  if (!bytes) return "—";
  const u = ["Б", "КБ", "МБ", "ГБ"];
  let i = 0;
  while (bytes >= 1024 && i < u.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(i ? 1 : 0).replace(".", ",")} ${u[i]}`;
}

export function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

const P = 'fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"';
export const icon = {
  play: `<svg viewBox="0 0 24 24" ${P}><path d="M7 4.5v15l12-7.5z"/></svg>`,
  edit: `<svg viewBox="0 0 24 24" ${P}><path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/></svg>`,
  refresh: `<svg viewBox="0 0 24 24" ${P}><path d="M20 11a8 8 0 0 0-14.8-4M4 4v4h4M4 13a8 8 0 0 0 14.8 4M20 20v-4h-4"/></svg>`,
  download: `<svg viewBox="0 0 24 24" ${P}><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>`,
  check: `<svg viewBox="0 0 24 24" ${P}><path d="m5 12.5 4.5 4.5L19 7"/></svg>`,
  x: `<svg viewBox="0 0 24 24" ${P}><path d="M6 6l12 12M18 6 6 18"/></svg>`,
  upload: `<svg viewBox="0 0 24 24" ${P}><path d="M12 16V4M7 9l5-5 5 5M5 20h14"/></svg>`,
  link: `<svg viewBox="0 0 24 24" ${P}><path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1"/></svg>`,
  film: `<svg viewBox="0 0 24 24" ${P}><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4"/></svg>`,
  trash: `<svg viewBox="0 0 24 24" ${P}><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>`,
  copy: `<svg viewBox="0 0 24 24" ${P}><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></svg>`,
  external: `<svg viewBox="0 0 24 24" ${P}><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>`,
  alert: `<svg viewBox="0 0 24 24" ${P}><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>`,
  info: `<svg viewBox="0 0 24 24" ${P}><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>`,
  more: `<svg viewBox="0 0 24 24" ${P}><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>`,
  plus: `<svg viewBox="0 0 24 24" ${P}><path d="M12 5v14M5 12h14"/></svg>`,
  up: `<svg viewBox="0 0 24 24" ${P}><path d="m6 15 6-6 6 6"/></svg>`,
  down: `<svg viewBox="0 0 24 24" ${P}><path d="m6 9 6 6 6-6"/></svg>`,
  stop: `<svg viewBox="0 0 24 24" ${P}><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`,
  mic: `<svg viewBox="0 0 24 24" ${P}><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>`,
  settings: `<svg viewBox="0 0 24 24" ${P}><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>`,
  text: `<svg viewBox="0 0 24 24" ${P}><path d="M5 6h14M12 6v13M9 19h6"/></svg>`,
  clock: `<svg viewBox="0 0 24 24" ${P}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
};

// ---------- toasts ----------
export function toast(message, type = "info", timeout = 4500) {
  const box = document.getElementById("toasts");
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = message;
  box.appendChild(t);
  setTimeout(() => t.remove(), timeout);
}

export function toastError(e) {
  toast(e?.message || String(e), "err", 7000);
}

// ---------- modal ----------
export function modal({ title, body, footer = "", wide = false, onClose } = {}) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">
      <div class="modal-head"><h2>${esc(title)}</h2><button class="btn ghost icon" data-close title="Закрыть">${icon.x}</button></div>
      <div class="modal-body"></div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ""}
    </div>`;
  const bodyEl = back.querySelector(".modal-body");
  if (typeof body === "string") bodyEl.innerHTML = body; else if (body) bodyEl.appendChild(body);
  const close = () => {
    back.querySelectorAll("video").forEach((v) => v.pause());
    back.remove();
    document.removeEventListener("keydown", onKey);
    onClose && onClose();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  back.querySelector("[data-close]").addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  document.body.appendChild(back);
  return { el: back, body: bodyEl, close };
}

export function confirmDialog(title, text, okLabel = "Подтвердить", danger = false) {
  return new Promise((resolve) => {
    let done = false;
    const m = modal({
      title,
      body: `<p class="muted">${esc(text)}</p>`,
      footer: `<button class="btn" data-no>Отмена</button><button class="btn ${danger ? "danger" : "primary"}" data-yes>${esc(okLabel)}</button>`,
      onClose: () => { if (!done) resolve(false); },
    });
    m.el.querySelector("[data-no]").onclick = () => m.close();
    m.el.querySelector("[data-yes]").onclick = () => { done = true; m.close(); resolve(true); };
  });
}

export async function copyText(text, what = "Текст") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(`${what} скопирован${what.endsWith("и") ? "ы" : ""} в буфер обмена`, "ok", 2500);
}

// ---------- statuses ----------
export const EPISODE_STATUS = {
  pending: ["Ожидает", ""],
  rendering: ["Создаётся", "warn"],
  ready: ["Готово", "ok"],
  outdated: ["Нужно пересоздать", "info"],
  error: ["Ошибка", "err"],
};
export const SUB_STATUS = {
  none: ["Субтитры: ожидание", ""],
  auto: ["Субтитры: авто", "ok"],
  edited: ["Субтитры: изменены", "info"],
  empty: ["Без речи", ""],
  disabled: ["Субтитры выкл.", ""],
};
export const PROJECT_STATUS = {
  draft: ["Черновик", ""],
  processing: ["Обработка", "warn"],
  ready: ["Готово", "ok"],
  error: ["Ошибка", "err"],
  cancelled: ["Отменено", ""],
};
export const PUBLISH_STATUS = {
  not_published: ["Не опубликовано", ""],
  scheduled: ["Запланировано", "info"],
  publishing: ["Публикуется", "warn"],
  inbox: ["Во «Входящих» TikTok", "info"],
  published: ["Опубликовано (API)", "ok"],
  manual: ["Опубликовано вручную", "ok"],
  failed: ["Ошибка публикации", "err"],
};

export function badge(map, key, extra = "") {
  const [label, cls] = map[key] || [key, ""];
  return `<span class="badge ${cls} ${extra}">${esc(label)}</span>`;
}

// ---------- job panel ----------
export function jobPanel(job, { showTitle = true } = {}) {
  if (!job) return "";
  const pct = Math.round(job.progress || 0);
  const eta = fmtEta(job.eta);
  const running = job.status === "running" || job.status === "queued";
  if (job.status === "failed") {
    return `<div class="alert err" data-job="${esc(job.id)}">${icon.alert}<div class="grow">
      <b>${esc(job.title)}: ошибка</b><div>${esc(job.error || "Неизвестная ошибка")}</div>
      ${job.error_detail ? `<details><summary>Технические подробности</summary><pre>${esc(job.error_detail)}</pre></details>` : ""}
      </div><button class="btn sm" data-retry="${esc(job.id)}">${icon.refresh}Повторить</button></div>`;
  }
  if (job.status === "cancelled") {
    return `<div class="alert warn" data-job="${esc(job.id)}">${icon.info}<div class="grow"><b>${esc(job.title)}: отменено</b></div>
      <button class="btn sm" data-retry="${esc(job.id)}">${icon.refresh}Продолжить</button></div>`;
  }
  if (!running) return "";
  return `<div class="job" data-job="${esc(job.id)}">
    <div class="job-top">
      ${job.status === "queued" ? `<span class="badge">В очереди</span>` : `<div class="spinner" style="width:18px;height:18px;border-width:2.5px"></div>`}
      <span class="job-stage">${esc(job.stage || job.title)}</span>
      <span class="job-pct">${pct}%</span>
      <button class="btn sm ghost" data-cancel="${esc(job.id)}" title="Отменить">${icon.stop}Отмена</button>
    </div>
    <div class="progress"><div style="width:${pct}%"></div></div>
    <div class="job-meta">
      ${showTitle ? `<span>${esc(job.title)}</span>` : ""}
      ${job.message ? `<span>${esc(job.message)}</span>` : ""}
      ${eta ? `<span>Осталось ${esc(eta)}</span>` : ""}
    </div>
  </div>`;
}

// ---------- timeline ----------
export function timelineHtml(items, total) {
  if (!items || !items.length || !total) return "";
  const segs = items.map((e) => {
    const w = (e.duration / total) * 100;
    const cls = [e.status || "", e.approved ? "approved" : ""].join(" ");
    const title = `Часть ${e.number ?? e.index + 1}: ${fmtDuration(e.start_time)}–${fmtDuration(e.end_time)}`;
    return `<div class="seg-item ${cls}" style="flex:${w} 0 0" title="${esc(title)}" data-seg="${esc(e.id ?? "")}">${w > 3 ? (e.number ?? e.index + 1) : ""}</div>`;
  }).join("");
  return `<div class="timeline">${segs}</div>
    <div class="timeline-scale"><span>00:00</span><span>${fmtDuration(total / 2)}</span><span>${fmtDuration(total)}</span></div>`;
}

export function mediaInfoHtml(m) {
  if (!m || !m.info) return "";
  const i = m.info;
  return `<dl class="kv">
    <dt>Длительность</dt><dd>${fmtDuration(i.duration)}</dd>
    <dt>Разрешение</dt><dd>${i.width}×${i.height}</dd>
    <dt>Частота кадров</dt><dd>${(Math.round(i.fps * 100) / 100).toString().replace(".", ",")} к/с${i.is_vfr ? ' <span class="badge info plain">переменная — будет выровнена</span>' : ""}</dd>
    <dt>Звук</dt><dd>${i.has_audio ? `есть (${esc(i.audio_codec || "")})` : '<span class="badge warn">нет звука</span>'}</dd>
    <dt>Размер</dt><dd>${fmtSize(i.size)}</dd>
  </dl>`;
}

export function emptyState(title, text, action = "") {
  return `<div class="empty">${icon.film}<h3>${esc(title)}</h3><p>${esc(text)}</p>${action ? `<div class="mt">${action}</div>` : ""}</div>`;
}
