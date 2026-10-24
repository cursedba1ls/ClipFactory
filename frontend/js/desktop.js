// Desktop integration: tells the window shell the UI is loaded, shows the version, saves downloads through
// the native "Save as" dialog and presents updates (found → downloaded → "restart and update").
import { api } from "./api.js";
import { $, esc, icon, modal, toast, toastError } from "./ui.js";
import { celebrate } from "./motion.js";

export let desktop = { desktop: false, version: "", can_save_dialog: false };

export async function initDesktop() {
  try {
    desktop = await api.get("/api/desktop/info");
    api.post("/api/desktop/ready").catch(() => {});
  } catch { /* old server or restarting */ }
  const foot = document.querySelector(".sidebar-foot");
  if (foot && desktop.version) foot.innerHTML = `ClipFactory ${esc(desktop.version)}<br><span class="faint">Локальная обработка · бесплатно</span>`;
  if (desktop.can_save_dialog) document.addEventListener("click", interceptDownload, true);
  pollUpdates();
  setInterval(pollUpdates, 5 * 60 * 1000);
}

// ---------------------------------------------------------------- downloads → native "Save as"
function interceptDownload(e) {
  const a = e.target.closest && e.target.closest("a[href]");
  if (!a) return;
  const href = a.getAttribute("href") || "";
  const isDownload = a.hasAttribute("download") || /\/(download|export)(\?|$)|\/subtitles\.(srt|ass)$/.test(href);
  if (!isDownload || !href.startsWith("/api/")) return;
  e.preventDefault();
  saveAs(href, a.getAttribute("download") || a.dataset.filename || defaultName(href));
}

// A file name for the "Save as" dialog when the link does not give one ("download" alone has no extension).
function defaultName(href) {
  const path = href.split("?")[0];
  if (/\/export$/.test(path)) return "ClipFactory.zip";
  if (/\/download$/.test(path)) return "ClipFactory.mp4";
  return path.split("/").pop() || "ClipFactory";
}

export async function saveAs(url, filename) {
  if (!desktop.can_save_dialog) { location.href = url; return; }
  try {
    const r = await api.post("/api/desktop/save", { url, filename: filename || defaultName(url) });
    if (r.saved) toast(`Сохранено: ${r.path}`, "ok", 6000);
  } catch (e) { toastError(e); }
}

export async function openFolder(target) {
  try { await api.post("/api/desktop/open", { target }); } catch (e) { toastError(e); }
}

// ---------------------------------------------------------------- updates
let lastState = null;
let shownJustUpdated = false;

export async function pollUpdates() {
  let st;
  try { st = await api.get("/api/update"); } catch { return null; }
  renderUpdateBar(st);
  if (st.just_updated && !shownJustUpdated) {
    shownJustUpdated = true;
    showWhatsNew(st.just_updated);
  }
  lastState = st;
  return st;
}

function notesHtml(text) {
  if (!text) return `<div class="muted">Исправления и улучшения.</div>`;
  return `<div class="notes">${esc(text).replace(/^### (.*)$/gm, "<b>$1</b>").replace(/^[-*] (.*)$/gm, "• $1").replace(/\n/g, "<br>")}</div>`;
}

function showWhatsNew(info) {
  const m = modal({
    title: `ClipFactory обновлён до версии ${esc(info.to)}`,
    body: `<div class="stack">${info.from ? `<div class="small muted">Предыдущая версия: ${esc(info.from)}. Все проекты и настройки сохранены.</div>` : ""}
      <div><b>Что нового</b></div>${notesHtml(info.notes)}</div>`,
    footer: `<button class="btn primary" data-ok>Отлично</button>`,
    onClose: () => api.post("/api/update/dismiss").catch(() => {}),
  });
  m.el.querySelector("[data-ok]").onclick = () => m.close();
  setTimeout(celebrate, 250);
}

function renderUpdateBar(st) {
  let bar = document.getElementById("update-bar");
  const show = st.installed_build && (st.state === "ready" || st.state === "available" || st.state === "downloading" || st.state === "installing");
  if (!show) { bar?.remove(); return; }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "update-bar";
    bar.className = "update-bar";
    document.getElementById("update-slot")?.append(bar);
  }
  const v = esc(st.available?.version || "");
  let html = "";
  if (st.state === "available") {
    html = `${icon.download}<div class="grow">Доступна новая версия <b>${v}</b>.</div>
      <button class="btn sm ghost" data-notes>Что нового</button><button class="btn sm primary" data-dl>Скачать</button>`;
  } else if (st.state === "downloading") {
    html = `<div class="spinner"></div><div class="grow">Загрузка версии <b>${v}</b>… ${Math.round(st.progress * 100)}%
      <div class="progress mt-s"><div style="width:${Math.round(st.progress * 100)}%"></div></div></div>`;
    setTimeout(pollUpdates, 1500);
  } else if (st.state === "ready") {
    html = `${icon.refresh}<div class="grow">Версия <b>${v}</b> готова к установке. Проекты и настройки сохранятся.</div>
      <button class="btn sm ghost" data-notes>Что нового</button><button class="btn sm primary" data-install>Перезапустить и обновить</button>`;
  } else {
    html = `<div class="spinner"></div><div class="grow">Устанавливается обновление ${v}. ClipFactory перезапустится автоматически.</div>`;
  }
  bar.innerHTML = html;
  bar.querySelector("[data-notes]")?.addEventListener("click", () => modal({
    title: `Что нового в версии ${v}`, body: notesHtml(st.available?.notes),
  }));
  bar.querySelector("[data-dl]")?.addEventListener("click", async () => {
    try { renderUpdateBar(await api.post("/api/update/download")); } catch (e) { toastError(e); }
  });
  bar.querySelector("[data-install]")?.addEventListener("click", () => installUpdate(st));
}

export async function installUpdate(st) {
  st = st || lastState;
  const running = st?.jobs_running || 0;
  const m = modal({
    title: `Обновить ClipFactory до версии ${esc(st?.available?.version || "")}?`,
    body: `<div class="stack"><div>Программа закроется, установит обновление и откроется снова — примерно за минуту.
      Все проекты, видео, черновики и настройки сохранятся; перед установкой делается резервная копия базы.</div>
      ${running ? `<div class="alert warn small">${icon.alert}<div>Сейчас идёт обработка (${running}). Она прервётся и продолжится автоматически после перезапуска.</div></div>` : ""}</div>`,
    footer: `<button class="btn" data-later>Позже</button><button class="btn primary" data-go>${icon.refresh}Перезапустить и обновить</button>`,
  });
  m.el.querySelector("[data-later]").onclick = () => m.close();
  m.el.querySelector("[data-go]").onclick = async () => {
    try {
      renderUpdateBar(await api.post("/api/update/install"));
      m.close();
    } catch (e) { toastError(e); }
  };
}
