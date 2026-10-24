// Video import widget: URL (yt-dlp) or local file upload, with progress, preview and media info.
import { api } from "./api.js";
import { $, $$, esc, fmtDuration, icon, jobPanel, mediaInfoHtml, toastError } from "./ui.js";

// "1:30", "01:02:03" or "90" → seconds; "" → null.
function parseTime(text) {
  const t = String(text || "").trim().replace(",", ".");
  if (!t) return null;
  const parts = t.split(":").map(Number);
  if (parts.some((x) => !Number.isFinite(x) || x < 0)) return NaN;
  return parts.reduce((acc, x) => acc * 60 + x, 0);
}

function fmtClock(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

const SEARCH_HINTS = {
  main: ["мультфильм", "animated short film", "короткометражный мультфильм", "Blender open movie"],
  gameplay: ["roblox obby gameplay no commentary", "minecraft parkour gameplay", "subway surfers gameplay", "gta ramp gameplay"],
};

// neededSeconds(): how much gameplay the series needs (the cartoon's length) — offered as the fragment length.
export function mediaImport({ kind, title, onChange, initial = null, compact = false, neededSeconds = null }) {
  const el = document.createElement("div");
  el.className = "stack";
  let media = initial;
  let timer = null;
  let tab = "url";
  let search = { q: "", cc: true, results: null, loading: false, error: null };
  let frag = { on: false, start: "", end: "" };
  let urlText = "";

  function draw() {
    if (media && (media.status === "pending" || media.status === "downloading")) return drawPending();
    if (media && media.status === "ready") return drawReady();
    el.innerHTML = `
      <div class="tabs"><button type="button" data-tab="url" class="${tab === "url" ? "on" : ""}">${icon.link} Ссылка</button>
        <button type="button" data-tab="file" class="${tab === "file" ? "on" : ""}">${icon.upload} Файл</button>
        <button type="button" data-tab="search" class="${tab === "search" ? "on" : ""}">${icon.film} YouTube</button></div>
      ${media && media.status === "error" ? `<div class="alert err">${icon.alert}<div class="grow">${esc(media.error || "Ошибка загрузки")}</div></div>` : ""}
      <div ${tab === "url" ? "" : 'class="hidden"'}>
        <div class="input-row"><input type="url" data-url placeholder="https://… ссылка на видео" value="${esc(urlText || media?.source_url || "")}">
          <button class="btn primary" type="button" data-load>Загрузить</button></div>
        ${fragHtml()}
        <div class="small faint mt-s">Используйте только видео, на которые у вас есть права. Защищённые (DRM, приватные, требующие входа) видео не скачиваются.</div>
      </div>
      <div ${tab === "search" ? "" : 'class="hidden"'}>
        <div class="input-row"><input type="search" data-q placeholder="${esc(SEARCH_HINTS[kind][0])}" value="${esc(search.q)}">
          <button class="btn primary" type="button" data-search>Найти</button></div>
        <div class="row mt-s" style="flex-wrap:wrap;gap:6px">${SEARCH_HINTS[kind].map((h) => `<button type="button" class="btn sm ghost" data-hint="${esc(h)}">${esc(h)}</button>`).join("")}</div>
        <label class="switch mt-s"><input type="checkbox" data-cc ${search.cc ? "checked" : ""}><span class="track"></span>
          <span class="small">Только Creative Commons (можно использовать с указанием автора)</span></label>
        ${fragHtml()}
        <div class="mt-s" data-results>${resultsHtml()}</div>
      </div>
      <div ${tab === "file" ? "" : 'class="hidden"'}>
        <div class="drop" data-drop>${icon.upload}<div><b>Перетащите видео сюда</b> или нажмите, чтобы выбрать файл</div>
          <div class="small faint">MP4, MOV, MKV, WEBM, AVI…</div></div>
        <input type="file" accept="video/*,.mkv,.webm,.mov,.avi,.flv,.wmv,.ts,.m4v" class="hidden" data-file>
        <div class="hidden mt-s" data-upprog><div class="progress"><div style="width:0%"></div></div><div class="small muted mt-s" data-uptext></div></div>
      </div>`;
    el.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { tab = b.dataset.tab; draw(); });
    bindFrag();
    const q = $("[data-q]", el);
    const runSearch = () => doSearch(q.value);
    $("[data-search]", el).onclick = runSearch;
    q.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); runSearch(); } };
    $("[data-cc]", el).onchange = (e) => { search.cc = e.target.checked; if (search.q) doSearch(search.q); };
    $$("[data-hint]", el).forEach((b) => b.onclick = () => { q.value = b.dataset.hint; runSearch(); });
    bindResults();
    const urlInput = $("[data-url]", el);
    urlInput.oninput = () => { urlText = urlInput.value; };
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
    if (c) c.onclick = async () => {
      c.disabled = true;
      try { await api.post(`/api/jobs/${c.dataset.cancel}/cancel`); } catch (e) { toastError(e); }
      refresh();
    };
  }

  function drawReady() {
    el.innerHTML = `<div class="media-preview">
        <video src="${esc(media.file_url)}" controls preload="metadata" muted></video>
        <div class="stack" style="gap:10px">
          <div class="row nowrap"><b style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(media.title)}">${esc(media.title)}</b>
            ${media.cached ? '<span class="badge info plain">из кэша</span>' : ""}</div>
          ${mediaInfoHtml(media)}
          ${kind === "main" && media.info && !media.info.has_audio ? `<div class="alert warn small">${icon.alert}<div>В видео нет звука: субтитры создать не получится.</div></div>` : ""}
          <div><button class="btn sm" type="button" data-replace>${icon.refresh}Заменить видео</button></div>
        </div></div>`;
    $("[data-replace]", el).onclick = () => { media = null; draw(); onChange && onChange(null); };
  }

  function fragHtml() {
    const need = neededSeconds ? neededSeconds() : null;
    return `<div class="mt-s" data-frag>
      <label class="switch"><input type="checkbox" data-frag-on ${frag.on ? "checked" : ""}><span class="track"></span>
        <span class="small">Скачать только фрагмент${kind === "gameplay" ? " (обрезать геймплей)" : ""}</span></label>
      <div class="row mt-s ${frag.on ? "" : "hidden"}" data-frag-opts>
        <span class="small">с</span><input type="text" style="width:90px" data-frag-start placeholder="0:00" value="${esc(frag.start)}">
        <span class="small">по</span><input type="text" style="width:90px" data-frag-end placeholder="конец" value="${esc(frag.end)}">
        ${need ? `<button type="button" class="btn sm" data-frag-fit title="Длина = длина мультфильма">Под длину мультфильма (${fmtClock(need)})</button>` : ""}
      </div></div>`;
  }

  function bindFrag() {
    $$("[data-frag]", el).forEach((box) => {
      const on = $("[data-frag-on]", box), st = $("[data-frag-start]", box), en = $("[data-frag-end]", box);
      on.onchange = () => { frag.on = on.checked; $$("[data-frag-opts]", el).forEach((o) => o.classList.toggle("hidden", !frag.on)); $$("[data-frag-on]", el).forEach((o) => (o.checked = frag.on)); };
      st.oninput = () => { frag.start = st.value; };
      en.oninput = () => { frag.end = en.value; };
      const fit = $("[data-frag-fit]", box);
      if (fit) fit.onclick = () => {
        const s0 = parseTime(frag.start) || 0;
        frag.start = fmtClock(s0);
        frag.end = fmtClock(s0 + Math.ceil(neededSeconds()) + 5);
        draw();
      };
    });
  }

  function fragment() {
    if (!frag.on) return {};
    const start = parseTime(frag.start), end = parseTime(frag.end);
    if (Number.isNaN(start) || Number.isNaN(end)) throw new Error("Время фрагмента: используйте формат 1:30 или секунды.");
    if (end != null && end - (start || 0) < 3) throw new Error("Фрагмент должен быть не короче 3 секунд.");
    return { start: start || null, end };
  }

  function resultsHtml() {
    if (search.loading) return `<div class="row"><div class="spinner"></div><span class="muted small">Поиск…</span></div>`;
    if (search.error) return `<div class="alert err small">${icon.alert}<div>${esc(search.error)}</div></div>`;
    if (!search.results) return `<div class="small faint">Найдите ${kind === "main" ? "мультфильм" : "геймплей"} и нажмите на него — видео скачается в программу.</div>`;
    if (!search.results.length) return `<div class="small muted">Ничего не найдено${search.cc ? " среди видео Creative Commons" : ""}.</div>`;
    return `<div class="yt-results">${search.results.map((r) => `
      <button type="button" class="yt-item" data-pick="${esc(r.url)}" title="${esc(r.title)}">
        <img src="${esc(r.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">
        <span class="yt-dur">${r.duration ? fmtDuration(r.duration) : ""}</span>
        <span class="yt-t">${esc(r.title)}</span><span class="small muted">${esc(r.channel)}</span>
      </button>`).join("")}</div>`;
  }

  function bindResults() {
    $$("[data-pick]", el).forEach((b) => b.onclick = () => loadUrl(b.dataset.pick));
  }

  async function doSearch(q) {
    search.q = q.trim();
    if (!search.q) return;
    search = { ...search, loading: true, error: null };
    $("[data-results]", el).innerHTML = resultsHtml();
    try {
      const params = new URLSearchParams({ q: search.q, cc: search.cc, limit: 12 });
      if (kind === "gameplay") params.set("min_seconds", 60);
      const data = await api.get(`/api/media/search?${params}`);
      search = { ...search, loading: false, results: data.results };
    } catch (e) {
      search = { ...search, loading: false, error: e.message };
    }
    const box = $("[data-results]", el);
    if (box) { box.innerHTML = resultsHtml(); bindResults(); }
  }

  async function loadUrl(url) {
    if (!url.trim()) return toastError(new Error("Вставьте ссылку на видео."));
    try {
      media = await api.post("/api/media/url", { url: url.trim(), kind, ...fragment() });
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
  return {
    el, get media() { return media; }, destroy() { clearInterval(timer); },
    // Re-render the import form (e.g. when the cartoon's length becomes known for "под длину мультфильма").
    redraw() { if (!media || media.status === "error") draw(); },
  };
}
