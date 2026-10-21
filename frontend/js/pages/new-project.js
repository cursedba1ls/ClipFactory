import { api } from "../api.js";
import { mediaImport } from "../media-import.js";
import { cropEditor, focusOf, sourceFromMedia } from "../crop-editor.js";
import { settingsForm } from "../settings-form.js";
import { clearDraft, debounce, draftTime, loadDraft, saveDraft } from "../drafts.js";
import { $, $$, esc, fmtDuration, icon, timelineHtml, toast, toastError, plural } from "../ui.js";

const DRAFT_KEY = "new-project";

export async function render(root) {
  const [settingsResp, library] = await Promise.all([api.get("/api/settings"), api.get("/api/media?kind=gameplay")]);
  const defaults = settingsResp.settings.defaults;
  const draft = loadDraft(DRAFT_KEY);
  const saved = draft ? draft.state : null;
  const selected = new Set(saved?.gameplay_ids || []);
  let cropDraft = saved?.crop || null;
  const savedMain = saved?.main_media_id
    ? await api.get(`/api/media/${saved.main_media_id}`).catch(() => null)
    : null;

  root.innerHTML = `
    <div class="page-head"><div><h1>Новый проект</h1>
      <div class="sub">Весь мультфильм будет разделён на последовательные части без пропусков, с геймплеем снизу и субтитрами</div></div></div>
    <div class="grid grid-2">
      <div class="card"><div class="card-head"><h2>1. Основное видео</h2><span class="hint">мультфильм, анимация, комментарий</span></div><div id="main-import"></div><div id="subs-file" class="mt hidden"></div></div>
      <div class="card"><div class="card-head"><h2>2. Геймплей</h2><span class="hint">например, Roblox — будет снизу без звука</span></div>
        <div id="gp-import"></div>
        <div class="mt" id="gp-lib-wrap"></div>
      </div>
    </div>
    <div class="card mt"><div class="card-head"><h2>3. Разбивка на части</h2><div class="spacer"></div><span class="hint" id="plan-hint"></span></div>
      <div id="plan"><div class="muted small">Загрузите основное видео, чтобы увидеть, как оно будет разделено.</div></div></div>
    <div class="card mt hidden" id="crop-card"><div class="card-head"><h2>Кадрирование</h2><span class="hint">автоматически или вручную — перетащите рамку</span></div><div id="crop"></div></div>
    <div class="card mt"><div class="card-head"><h2>4. Настройки</h2></div>
      <label class="field" style="margin-bottom:16px"><span>Название проекта (необязательно)</span>
        <input type="text" id="title" maxlength="200" placeholder="По умолчанию — название видео" value="${esc(saved?.title || "")}"></label>
      <div id="settings"></div></div>
    <div class="row mt" style="justify-content:flex-end">
      <span class="muted small" id="create-hint"></span>
      <button class="btn primary lg" id="create" disabled>${icon.play}Создать видео</button></div>`;

  const form = settingsForm(saved?.settings || defaults, { showCaption: true });
  $("#settings", root).appendChild(form.el);

  let cropEd = null, cropKey = "";
  function updateCrop() {
    const m = mainImp.media;
    const g = lib.find((x) => selected.has(x.id) && x.status === "ready") || (gpImp?.media?.status === "ready" ? gpImp.media : null);
    const card = $("#crop-card", root);
    if (!m || m.status !== "ready" || !g) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const key = `${m.id}|${g.id}`;
    const s = form.getValue();
    if (key === cropKey && cropEd) { cropEd.setLayout(s.layout); cropEd.setDivider(s.divider); return; }
    cropKey = key;
    const prev = cropEd ? cropEd.getValue() : (cropDraft || defaults.crop || {});
    $("#crop", root).innerHTML = "";
    cropEd = cropEditor({
      main: sourceFromMedia(m), game: sourceFromMedia(g, 0.1), layout: s.layout, crop: prev, divider: s.divider,
      auto: { main_x: focusOf(m).x, main_y: focusOf(m).y, game_x: focusOf(g).x, game_y: focusOf(g).y },
      hint: "«Автоматически»: чёрные полосы убираются, а рамка в каждой части ставится туда, где происходит действие. Зелёная пунктирная рамка — примерное положение для всего видео.",
    });
    $("#crop", root).appendChild(cropEd.el);
  }

  async function updateSubs() {
    const box = $("#subs-file", root);
    const m = mainImp.media;
    if (!m || m.status !== "ready") { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    let info = null;
    try { info = (await api.get(`/api/media/${m.id}`)).subtitles_file; } catch { /* ignore */ }
    box.innerHTML = `<div class="field-label" style="margin-bottom:6px">Субтитры из файла (необязательно)</div>
      ${info ? `<div class="row"><span class="badge ok">${esc(info.filename)} · ${info.cues} фраз</span>
          <button class="btn sm ghost" type="button" data-subs-del>${icon.trash}Убрать</button></div>
        <div class="small faint mt-s">Субтитры возьмутся из файла — распознавание речи не понадобится.</div>`
      : `<button class="btn sm" type="button" data-subs-pick>${icon.upload}Загрузить .srt / .vtt</button>
        <input type="file" accept=".srt,.vtt" class="hidden" data-subs-input>
        <div class="small faint mt-s">Если файла нет, субтитры будут созданы автоматически из речи в видео.</div>`}`;
    const pick = box.querySelector("[data-subs-pick]");
    if (pick) {
      const input = box.querySelector("[data-subs-input]");
      pick.onclick = () => input.click();
      input.onchange = async () => {
        const f = input.files[0];
        if (!f) return;
        try {
          const resp = await fetch(`/api/media/${m.id}/subtitles?filename=${encodeURIComponent(f.name)}`, { method: "POST", body: f });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.detail || "Ошибка загрузки субтитров");
          toast(`Субтитры загружены: ${data.cues} фраз`, "ok");
        } catch (e) { toastError(e); }
        updateSubs();
      };
    }
    const del = box.querySelector("[data-subs-del]");
    if (del) del.onclick = async () => { try { await api.del(`/api/media/${m.id}/subtitles`); } catch (e) { toastError(e); } updateSubs(); };
  }

  const mainImp = mediaImport({
    kind: "main",
    initial: savedMain,
    onChange: () => { updatePlan(); updateButton(); updateCrop(); updateSubs(); schedulePersist(); },
  });
  $("#main-import", root).appendChild(mainImp.el);

  const gpImp = mediaImport({
    kind: "gameplay",
    onChange: (m) => {
      if (m && m.status !== "error") selected.add(m.id);
      refreshLibrary().then(updateCrop);
      updateButton();
      schedulePersist();
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
      updateCrop();
      schedulePersist();
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
  form.el.addEventListener("change-settings", () => { updatePlan(); updateCrop(); });

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
        settings: { ...form.getValue(), ...(cropEd ? { crop: cropEd.getValue() } : {}) },
        start: true,
      });
      clearDraft(DRAFT_KEY);
      toast("Проект создан, обработка запущена", "ok");
      location.hash = `#/project/${p.id}`;
    } catch (e) {
      toastError(e);
      btn.disabled = false;
    }
  };

  // ----- drafts: everything typed or chosen here is kept until the project is created -----
  function currentDraft() {
    return {
      title: $("#title", root).value,
      settings: form.getValue(),
      main_media_id: mainImp.media?.id || null,
      gameplay_ids: [...selected],
      crop: cropEd ? cropEd.getValue() : cropDraft,
    };
  }
  function persist() {
    if (!$("#title", root)) return;  // the page was left; never write another page's state
    const d = currentDraft();
    const empty = !d.title.trim() && !d.main_media_id && !d.gameplay_ids.length;
    if (empty) clearDraft(DRAFT_KEY);
    else saveDraft(DRAFT_KEY, d);
  }
  const schedulePersist = debounce(persist);
  const draftEvents = ["input", "change", "pointerup", "click", "change-settings"];
  for (const type of draftEvents) root.addEventListener(type, schedulePersist);

  if (draft && (saved.main_media_id || saved.gameplay_ids?.length || saved.title)) {
    const banner = document.createElement("div");
    banner.className = "alert info";
    banner.style.marginBottom = "16px";
    banner.innerHTML = `${icon.info}<div class="grow">Восстановлен черновик от ${esc(draftTime(draft))}. Всё, что вы вводили, сохранено.</div>
      <button class="btn sm ghost" type="button" data-fresh>Начать заново</button>`;
    root.querySelector(".page-head").after(banner);
    banner.querySelector("[data-fresh]").onclick = () => { clearDraft(DRAFT_KEY); location.reload(); };
  }

  updateButton();
  updatePlan();
  updateCrop();
  updateSubs();
  return () => {
    for (const type of draftEvents) root.removeEventListener(type, schedulePersist);
    mainImp.destroy();
    gpImp.destroy();
  };
}
