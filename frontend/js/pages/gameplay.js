import { api } from "../api.js";
import { mediaImport } from "../media-import.js";
import { $$, confirmDialog, emptyState, esc, fmtDuration, fmtSize, icon, toast, toastError } from "../ui.js";

export async function render(root) {
  root.innerHTML = `
    <div class="page-head"><div><h1>Геймплей</h1><div class="sub">Библиотека фоновых видео (Roblox и др.) для нижней половины экрана</div></div></div>
    <div class="grid" style="grid-template-columns:minmax(0,420px) minmax(0,1fr);align-items:start" id="cols">
      <div class="card"><div class="card-head"><h2>Добавить геймплей</h2></div><div id="import"></div>
        <div class="small faint mt">Чёрные кадры в начале и конце клипа определяются автоматически и не попадут в видео. Если клип короче части — он зациклится.</div></div>
      <div class="card"><div class="card-head"><h2>Библиотека</h2><div class="spacer"></div><span class="hint" id="count"></span></div><div id="list"></div></div>
    </div>`;
  if (window.matchMedia("(max-width: 960px)").matches) root.querySelector("#cols").style.gridTemplateColumns = "1fr";

  let imp;
  function newImport() {
    const box = root.querySelector("#import");
    box.innerHTML = "";
    imp = mediaImport({
      kind: "gameplay",
      onChange: (m) => {
        load();
        if (m && m.status === "ready") { toast("Геймплей добавлен в библиотеку", "ok"); setTimeout(newImport, 600); }
      },
    });
    box.appendChild(imp.el);
  }
  newImport();

  function usedLabel(m) {
    if (!m.info || !m.usable_range) return "";
    const [a, b] = m.usable_range;
    const whole = a <= 0.05 && b >= m.info.video_duration - 0.05;
    return whole ? "" : ` · в проектах используется ${fmtDuration(a)}–${fmtDuration(b)}`;
  }

  // Trim panel: pick the part of a long clip that the parts will use (the rest is never used).
  function openTrim(m) {
    const box = root.querySelector(`[data-trim-panel="${m.id}"]`);
    if (!box) return;
    if (!box.classList.contains("hidden")) { box.classList.add("hidden"); box.innerHTML = ""; return; }
    const total = m.info.video_duration;
    const [a0, b0] = m.usable_range || [0, total];
    let start = m.trim_start ?? a0, end = m.trim_end ?? b0;
    box.classList.remove("hidden");
    box.innerHTML = `<div class="card" style="margin-top:8px">
      <div class="row" style="justify-content:space-between"><b>Обрезка: какую часть клипа брать</b>
        <span class="small muted" data-span></span></div>
      <video src="${esc(m.file_url)}" controls preload="metadata" playsinline style="width:100%;max-height:260px;background:#000;border-radius:8px;margin-top:10px" data-trim-video></video>
      <div class="stack mt-s">
        <label class="field"><span>Начало: <b data-s-label></b></span>
          <input type="range" min="0" max="${total}" step="0.5" value="${start}" data-trim-start></label>
        <label class="field"><span>Конец: <b data-e-label></b></span>
          <input type="range" min="0" max="${total}" step="0.5" value="${end}" data-trim-end></label>
      </div>
      <div class="small faint mt-s">Перемещайте ползунки и смотрите предпросмотр. Минимум ${3} секунды. Фрагмент можно поставить на начало мультфильма или сделать короче, чем клип.</div>
      <div class="row mt" style="justify-content:flex-end">
        <button class="btn sm ghost" data-trim-reset>Весь клип</button>
        <button class="btn sm" data-trim-cancel>Отмена</button>
        <button class="btn sm primary" data-trim-save>${icon.check}Сохранить обрезку</button>
      </div></div>`;
    const sEl = box.querySelector("[data-trim-start]"), eEl = box.querySelector("[data-trim-end]");
    const video = box.querySelector("[data-trim-video]");
    const paint = () => {
      start = Number(sEl.value); end = Number(eEl.value);
      if (end - start < 3) { if (document.activeElement === sEl) end = Math.min(total, start + 3); else start = Math.max(0, end - 3); sEl.value = start; eEl.value = end; }
      box.querySelector("[data-s-label]").textContent = fmtDuration(start);
      box.querySelector("[data-e-label]").textContent = fmtDuration(end);
      box.querySelector("[data-span]").textContent = `${fmtDuration(end - start)} из ${fmtDuration(total)}`;
    };
    sEl.oninput = () => { paint(); video.currentTime = start; };
    eEl.oninput = () => { paint(); video.currentTime = Math.max(start, end - 2); };
    paint();
    box.querySelector("[data-trim-cancel]").onclick = () => { box.classList.add("hidden"); box.innerHTML = ""; };
    box.querySelector("[data-trim-reset]").onclick = async () => {
      try { await api.patch(`/api/media/${m.id}`, { reset_trim: true }); toast("Обрезка снята: используется весь клип", "ok"); load(); }
      catch (e) { toastError(e); }
    };
    box.querySelector("[data-trim-save]").onclick = async () => {
      const whole = start <= a0 + 0.05 && end >= b0 - 0.05 && a0 <= 0.05 && b0 >= total - 0.05;
      try {
        if (whole) await api.patch(`/api/media/${m.id}`, { reset_trim: true });
        else await api.patch(`/api/media/${m.id}`, { trim_start: start, trim_end: end });
        toast("Обрезка сохранена. Части, которые его используют, отмечены «Нужно пересоздать»", "ok", 6000);
        load();
      } catch (e) { toastError(e); }
    };
  }

  async function load() {
    const list = await api.get("/api/media?kind=gameplay");
    root.querySelector("#count").textContent = list.length ? `${list.length} шт.` : "";
    const box = root.querySelector("#list");
    if (!list.length) {
      box.innerHTML = emptyState("Библиотека пуста", "Добавьте хотя бы один клип геймплея по ссылке или с компьютера.");
      return list;
    }
    box.innerHTML = `<div class="stack">${list.map((m) => `
      <div class="row nowrap" style="gap:14px;padding:10px;border:1px solid var(--border);border-radius:12px;background:var(--surface)">
        ${m.file_url ? `<video src="${esc(m.file_url)}#t=2" poster="/api/media/${esc(m.id)}/frame?t=2&w=320" preload="none" muted loop playsinline style="width:150px;aspect-ratio:16/9;object-fit:cover;border-radius:8px;background:#000" data-hover></video>`
          : `<div style="width:150px;aspect-ratio:16/9;display:grid;place-items:center;background:#000;border-radius:8px">${m.status === "error" ? icon.alert : '<div class="spinner"></div>'}</div>`}
        <div style="flex:1;min-width:0" class="stack">
          <div style="gap:4px" class="stack">
            <input type="text" value="${esc(m.title)}" data-rename="${esc(m.id)}" maxlength="200" style="height:32px;font-weight:600">
            ${m.status === "ready" ? `<div class="small muted">${fmtDuration(m.info?.duration)} · ${m.info?.width}×${m.info?.height} · ${fmtSize(m.info?.size)}
              ${usedLabel(m)}</div>`
              : m.status === "error" ? `<div class="small" style="color:var(--err)">${esc(m.error || "Ошибка")}</div>` : `<div class="small muted">Загружается…</div>`}
            ${m.status === "ready" && m.info?.video_duration > 60 ? `<div><button class="btn sm" data-trim-open="${esc(m.id)}">${icon.edit}Обрезать фрагмент</button></div>` : ""}
          </div>
        </div>
        <button class="btn sm icon danger" data-del="${esc(m.id)}" title="Удалить">${icon.trash}</button>
      </div>
      <div class="hidden" data-trim-panel="${esc(m.id)}"></div>`).join("")}</div>`;
    $$("[data-hover]", box).forEach((v) => {
      v.onmouseenter = () => v.play().catch(() => {});
      v.onmouseleave = () => v.pause();
    });
    $$("[data-trim-open]", box).forEach((b) => b.onclick = () => openTrim(list.find((x) => x.id === b.dataset.trimOpen)));
    $$("[data-rename]", box).forEach((inp) => inp.onchange = async () => {
      try { await api.patch(`/api/media/${inp.dataset.rename}`, { title: inp.value }); toast("Название сохранено", "ok", 2000); }
      catch (e) { toastError(e); }
    });
    $$("[data-del]", box).forEach((b) => b.onclick = async () => {
      if (!(await confirmDialog("Удалить клип?", "Файл будет удалён с диска. Уже созданные части не изменятся, но при пересоздании для них будет выбран другой геймплей.", "Удалить", true))) return;
      try { await api.del(`/api/media/${b.dataset.del}`); toast("Клип удалён", "ok"); load(); } catch (e) { toastError(e); }
    });
    return list;
  }
  await load();
  const timer = setInterval(async () => {
    try {
      const list = await api.get("/api/media?kind=gameplay");
      if (list.some((m) => m.status === "pending" || m.status === "downloading")) load();
    } catch { /* ignore */ }
  }, 4000);
  return () => { clearInterval(timer); imp && imp.destroy(); };
}
