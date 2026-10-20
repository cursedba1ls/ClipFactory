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
        ${m.file_url ? `<video src="${esc(m.file_url)}#t=2" preload="metadata" muted loop playsinline style="width:150px;aspect-ratio:16/9;object-fit:cover;border-radius:8px;background:#000" data-hover></video>`
          : `<div style="width:150px;aspect-ratio:16/9;display:grid;place-items:center;background:#000;border-radius:8px">${m.status === "error" ? icon.alert : '<div class="spinner"></div>'}</div>`}
        <div style="flex:1;min-width:0" class="stack">
          <div style="gap:4px" class="stack">
            <input type="text" value="${esc(m.title)}" data-rename="${esc(m.id)}" maxlength="200" style="height:32px;font-weight:600">
            ${m.status === "ready" ? `<div class="small muted">${fmtDuration(m.info?.duration)} · ${m.info?.width}×${m.info?.height} · ${fmtSize(m.info?.size)}
              ${m.usable_start > 0.05 || (m.info && m.usable_end < m.info.video_duration - 0.05) ? ` · используется ${fmtDuration(m.usable_start)}–${fmtDuration(m.usable_end)}` : ""}</div>`
              : m.status === "error" ? `<div class="small" style="color:var(--err)">${esc(m.error || "Ошибка")}</div>` : `<div class="small muted">Загружается…</div>`}
          </div>
        </div>
        <button class="btn sm icon danger" data-del="${esc(m.id)}" title="Удалить">${icon.trash}</button>
      </div>`).join("")}</div>`;
    $$("[data-hover]", box).forEach((v) => {
      v.onmouseenter = () => v.play().catch(() => {});
      v.onmouseleave = () => v.pause();
    });
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
