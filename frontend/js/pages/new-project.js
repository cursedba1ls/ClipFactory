import { api } from "../api.js";
import { mediaImport } from "../media-import.js";
import { settingsForm } from "../settings-form.js";
import { $, $$, esc, fmtDuration, icon, timelineHtml, toast, toastError, plural } from "../ui.js";

export async function render(root) {
  const [settingsResp, library] = await Promise.all([api.get("/api/settings"), api.get("/api/media?kind=gameplay")]);
  const defaults = settingsResp.settings.defaults;
  const selected = new Set();

  root.innerHTML = `
    <div class="page-head"><div><h1>Новый проект</h1>
      <div class="sub">Весь мультфильм будет разделён на последовательные части без пропусков, с геймплеем снизу и субтитрами</div></div></div>
    <div class="grid grid-2">
      <div class="card"><div class="card-head"><h2>1. Основное видео</h2><span class="hint">мультфильм, анимация, комментарий</span></div><div id="main-import"></div></div>
      <div class="card"><div class="card-head"><h2>2. Геймплей</h2><span class="hint">например, Roblox — будет снизу без звука</span></div>
        <div id="gp-import"></div>
        <div class="mt" id="gp-lib-wrap"></div>
      </div>
    </div>
    <div class="card mt"><div class="card-head"><h2>3. Разбивка на части</h2><div class="spacer"></div><span class="hint" id="plan-hint"></span></div>
      <div id="plan"><div class="muted small">Загрузите основное видео, чтобы увидеть, как оно будет разделено.</div></div></div>
    <div class="card mt"><div class="card-head"><h2>4. Настройки</h2></div>
      <label class="field" style="margin-bottom:16px"><span>Название проекта (необязательно)</span>
        <input type="text" id="title" maxlength="200" placeholder="По умолчанию — название видео"></label>
      <div id="settings"></div></div>
    <div class="row mt" style="justify-content:flex-end">
      <span class="muted small" id="create-hint"></span>
      <button class="btn primary lg" id="create" disabled>${icon.play}Создать видео</button></div>`;

  const form = settingsForm(defaults, { showCaption: true });
  $("#settings", root).appendChild(form.el);

  const mainImp = mediaImport({ kind: "main", onChange: () => { updatePlan(); updateButton(); } });
  $("#main-import", root).appendChild(mainImp.el);

  const gpImp = mediaImport({
    kind: "gameplay",
    onChange: (m) => {
      if (m && m.status !== "error") selected.add(m.id);
      refreshLibrary();
      updateButton();
    },
  });
  $("#gp-import", root).appendChild(gpImp.el);

  let lib = library;
  function drawLibrary() {
    const wrap = $("#gp-lib-wrap", root);
    const items = lib.filter((m) => m.status !== "error");
    if (!items.length) { wrap.innerHTML = ""; return; }
    wrap.innerHTML = `<div class="field-label" style="margin-bottom:8px">Библиотека геймплея — выберите один или несколько клипов</div>
      <div class="lib-list">${items.map((m) => `
        <div class="lib-item ${selected.has(m.id) ? "on" : ""}" data-id="${esc(m.id)}">
          <div class="check">${icon.check}</div>
          ${m.file_url ? `<video src="${esc(m.file_url)}#t=1" preload="metadata" muted></video>` : `<div class="spinner"></div>`}
          <div class="grow"><div class="t">${esc(m.title)}</div>
            <div class="small muted">${m.status === "ready" ? `${fmtDuration(m.info?.duration)} · ${m.info?.width}×${m.info?.height}` : "загружается…"}</div></div>
        </div>`).join("")}</div>
      <div class="small faint mt-s">Если выбрано несколько клипов, они будут чередоваться между частями. Короткий геймплей автоматически зацикливается.</div>`;
    $$(".lib-item", wrap).forEach((it) => it.onclick = () => {
      const id = it.dataset.id;
      selected.has(id) ? selected.delete(id) : selected.add(id);
      it.classList.toggle("on", selected.has(id));
      updateButton();
    });
  }
  async function refreshLibrary() {
    try { lib = await api.get("/api/media?kind=gameplay"); } catch { /* ignore */ }
    drawLibrary();
  }
  drawLibrary();

  let planTimer = null;
  async function updatePlan() {
    clearTimeout(planTimer);
    planTimer = setTimeout(async () => {
      const m = mainImp.media;
      const box = $("#plan", root), hint = $("#plan-hint", root);
      if (!m || m.status !== "ready") {
        box.innerHTML = `<div class="muted small">${m ? "Ожидание загрузки видео…" : "Загрузите основное видео, чтобы увидеть, как оно будет разделено."}</div>`;
        hint.textContent = "";
        return;
      }
      const s = form.getValue();
      if (form.validate()) { box.innerHTML = `<div class="alert warn">${esc(form.validate())}</div>`; return; }
      try {
        const plan = await api.get(`/api/media/${m.id}/plan?episode_seconds=${s.episode_seconds}&min_last_seconds=${s.min_last_seconds ?? 3}`);
        const items = plan.episodes.map((e) => ({ ...e, number: e.index + 1 }));
        hint.textContent = `${items.length} ${plural(items.length, "часть", "части", "частей")} · всего ${fmtDuration(plan.duration)}`;
        box.innerHTML = timelineHtml(items, plan.duration) +
          `<div class="plan-list">${items.map((e) => `<span>Часть ${e.number} · ${esc(e.label)}${Math.abs(e.duration - s.episode_seconds) > 0.5 ? ` (${Math.round(e.duration)} с)` : ""}</span>`).join("")}</div>
          ${plan.approximate ? `<div class="small faint mt-s">У видео переменная частота кадров: перед нарезкой таймлайн будет выровнен, границы могут сдвинуться на доли секунды.</div>` : ""}`;
      } catch (e) { box.innerHTML = `<div class="alert err">${esc(e.message)}</div>`; }
    }, 150);
  }
  form.el.addEventListener("change-settings", updatePlan);

  function updateButton() {
    const m = mainImp.media;
    const gpOk = [...selected].length > 0;
    const btn = $("#create", root), hint = $("#create-hint", root);
    let msg = "";
    if (!m || m.status === "error") msg = "Добавьте основное видео";
    else if (!gpOk) msg = "Выберите или загрузите геймплей";
    btn.disabled = !!msg;
    hint.textContent = msg || (m.status !== "ready" ? "Видео ещё загружается — обработка начнётся после загрузки" : "");
  }

  $("#create", root).onclick = async () => {
    const err = form.validate();
    if (err) return toastError(new Error(err));
    const btn = $("#create", root);
    btn.disabled = true;
    try {
      const p = await api.post("/api/projects", {
        title: $("#title", root).value.trim(),
        main_media_id: mainImp.media.id,
        gameplay_media_ids: [...selected],
        settings: form.getValue(),
        start: true,
      });
      toast("Проект создан, обработка запущена", "ok");
      location.hash = `#/project/${p.id}`;
    } catch (e) {
      toastError(e);
      btn.disabled = false;
    }
  };

  updateButton();
  return () => { mainImp.destroy(); gpImp.destroy(); };
}
