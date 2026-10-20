// Video import widget: URL (yt-dlp) or local file upload, with progress, preview and media info.
import { api } from "./api.js";
import { $, esc, icon, jobPanel, mediaInfoHtml, toastError } from "./ui.js";

export function mediaImport({ kind, title, onChange, initial = null, compact = false }) {
  const el = document.createElement("div");
  el.className = "stack";
  let media = initial;
  let timer = null;
  let tab = "url";

  function draw() {
    if (media && (media.status === "pending" || media.status === "downloading")) return drawPending();
    if (media && media.status === "ready") return drawReady();
    el.innerHTML = `
      <div class="tabs"><button type="button" data-tab="url" class="${tab === "url" ? "on" : ""}">${icon.link} По ссылке</button>
        <button type="button" data-tab="file" class="${tab === "file" ? "on" : ""}">${icon.upload} Файл с компьютера</button></div>
      ${media && media.status === "error" ? `<div class="alert err">${icon.alert}<div class="grow">${esc(media.error || "Ошибка загрузки")}</div></div>` : ""}
      <div ${tab === "url" ? "" : 'class="hidden"'}>
        <div class="input-row"><input type="url" data-url placeholder="https://… ссылка на видео" value="${esc(media?.source_url || "")}">
          <button class="btn primary" type="button" data-load>Загрузить</button></div>
        <div class="small faint mt-s">Используйте только видео, на которые у вас есть права. Защищённые (DRM, приватные, требующие входа) видео не скачиваются.</div>
      </div>
      <div ${tab === "file" ? "" : 'class="hidden"'}>
        <div class="drop" data-drop>${icon.upload}<div><b>Перетащите видео сюда</b> или нажмите, чтобы выбрать файл</div>
          <div class="small faint">MP4, MOV, MKV, WEBM, AVI…</div></div>
        <input type="file" accept="video/*,.mkv,.webm,.mov,.avi,.flv,.wmv,.ts,.m4v" class="hidden" data-file>
        <div class="hidden mt-s" data-upprog><div class="progress"><div style="width:0%"></div></div><div class="small muted mt-s" data-uptext></div></div>
      </div>`;
    el.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { tab = b.dataset.tab; draw(); });
    const urlInput = $("[data-url]", el);
    $("[data-load]", el).onclick = () => loadUrl(urlInput.value);
    urlInput.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); loadUrl(urlInput.value); } };
    const drop = $("[data-drop]", el), fileInput = $("[data-file]", el);
    drop.onclick = () => fileInput.click();
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); };
    fileInput.onchange = () => { if (fileInput.files[0]) upload(fileInput.files[0]); };
  }

  function drawPending() {
    el.innerHTML = `<div class="small muted">${esc(media.source_url || "")}</div>
      ${media.job ? jobPanel(media.job, { showTitle: false }) : `<div class="row"><div class="spinner"></div><span class="muted">Ожидание загрузки…</span></div>`}`;
    const c = el.querySelector("[data-cancel]");
    if (c) c.onclick = async () => { await api.post(`/api/jobs/${c.dataset.cancel}/cancel`); refresh(); };
  }

  function drawReady() {
    el.innerHTML = `<div class="media-preview">
        <video src="${esc(media.file_url)}" controls preload="metadata" muted></video>
        <div class="stack" style="gap:10px">
          <div class="row nowrap"><b style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(media.title)}">${esc(media.title)}</b>
            ${media.cached ? '<span class="badge info plain">из кэша</span>' : ""}</div>
          ${mediaInfoHtml(media)}
          ${kind === "main" && media.info && !media.info.has_audio ? `<div class="alert warn small">${icon.alert}<div>В видео нет звука: субтитры создать не получится, части будут без звука оригинала.</div></div>` : ""}
          <div><button class="btn sm" type="button" data-replace>${icon.refresh}Заменить видео</button></div>
        </div></div>`;
    $("[data-replace]", el).onclick = () => { media = null; draw(); onChange && onChange(null); };
  }

  async function loadUrl(url) {
    if (!url.trim()) return toastError(new Error("Вставьте ссылку на видео."));
    try {
      media = await api.post("/api/media/url", { url: url.trim(), kind });
      draw();
      onChange && onChange(media);
      poll();
    } catch (e) { toastError(e); }
  }

  async function upload(file) {
    const prog = $("[data-upprog]", el);
    prog.classList.remove("hidden");
    const bar = prog.querySelector(".progress > div"), txt = $("[data-uptext]", el);
    try {
      media = await api.upload(kind, file, (f) => {
        bar.style.width = `${Math.round(f * 100)}%`;
        txt.textContent = f < 1 ? `Загрузка файла: ${Math.round(f * 100)}%` : "Анализ видео…";
      });
      draw();
      onChange && onChange(media);
    } catch (e) {
      prog.classList.add("hidden");
      toastError(e);
    }
  }

  async function refresh() {
    if (!media) return;
    try {
      media = await api.get(`/api/media/${media.id}`);
      draw();
      onChange && onChange(media);
    } catch (e) { toastError(e); }
  }

  function poll() {
    clearInterval(timer);
    timer = setInterval(async () => {
      if (!media || !el.isConnected) return clearInterval(timer);
      await refresh();
      if (media && media.status !== "pending" && media.status !== "downloading") clearInterval(timer);
    }, 1200);
  }

  draw();
  if (media && media.status !== "ready" && media.status !== "error") poll();
  return { el, get media() { return media; }, destroy() { clearInterval(timer); } };
}
