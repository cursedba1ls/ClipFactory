import { api } from "../api.js";
import { openEditor } from "../editor.js";
import { settingsForm } from "../settings-form.js";
import {
  $, $$, badge, confirmDialog, EPISODE_STATUS, esc, fmtDuration, fmtSecondsShort, icon, jobPanel, mediaInfoHtml,
  modal, plural, PROJECT_STATUS, SUB_STATUS, timelineHtml, toast, toastError,
} from "../ui.js";

export async function render(root, [projectId]) {
  let project = await api.get(`/api/projects/${projectId}`);
  const cardKeys = new Map();

  root.innerHTML = `
    <div class="page-head">
      <div style="flex:1;min-width:260px">
        <input class="title-edit" id="title" value="${esc(project.title)}" maxlength="200" title="Нажмите, чтобы переименовать">
        <div class="row" style="gap:8px" id="head-meta"></div>
      </div>
      <div class="btn-group">
        <button class="btn primary" id="build">${icon.play}Создать все части</button>
        <a class="btn" id="zip" href="/api/projects/${esc(projectId)}/export">${icon.download}Скачать все (ZIP)</a>
        <div class="menu"><button class="btn icon" id="more" title="Ещё">${icon.more}</button><div class="menu-list hidden" id="menu">
          <button data-act="approve-all">${icon.check}Одобрить все готовые части</button>
          <button data-act="rerender">${icon.refresh}Пересоздать все части</button>
          <button data-act="retranscribe">${icon.mic}Распознать речь заново</button>
          <button data-act="captions">${icon.text}Обновить подписи и хештеги</button>
          <button data-act="settings">${icon.settings}Настройки проекта</button>
          <button data-act="delete" class="danger">${icon.trash}Удалить проект</button>
        </div></div>
      </div>
    </div>
    <div id="jobs"></div>
    <div id="warnings"></div>
    <div class="grid grid-2 mt" id="info"></div>
    <div class="card mt"><div class="card-head"><h2>Таймлайн серии</h2><div class="spacer"></div><span class="hint" id="tl-hint"></span></div><div id="timeline"></div></div>
    <div class="card-head mt" style="margin-top:26px"><h2>Части</h2><div class="spacer"></div><span class="hint" id="ep-hint"></span></div>
    <div class="episodes" id="episodes"></div>`;

  const titleInput = $("#title", root);
  titleInput.addEventListener("change", async () => {
    try { project = await api.patch(`/api/projects/${projectId}`, { title: titleInput.value }); toast("Название сохранено", "ok", 2000); }
    catch (e) { toastError(e); }
  });

  // ---------- rendering ----------
  function drawHead() {
    const c = project.counts;
    $("#head-meta", root).innerHTML = `${badge(PROJECT_STATUS, project.status)}
      ${project.timeline ? `<span class="muted small">${fmtDuration(project.timeline.duration)} исходника</span>` : ""}
      ${c.total ? `<span class="muted small">· ${c.total} ${plural(c.total, "часть", "части", "частей")} по ${project.settings.episode_seconds} с</span>` : ""}
      ${project.transcript_language ? `<span class="muted small">· речь: ${project.transcript_language === "ru" ? "русский" : project.transcript_language === "en" ? "английский" : esc(project.transcript_language)}</span>` : ""}`;
    const busy = project.active_jobs.length > 0;
    const b = $("#build", root);
    b.disabled = busy;
    const allReady = c.total && c.ready === c.total;
    b.innerHTML = busy ? `<div class="spinner" style="width:16px;height:16px;border-width:2px"></div>Идёт обработка`
      : allReady ? `${icon.check}Все части готовы` : `${icon.play}Создать все части`;
    if (allReady && !busy) b.disabled = true;
    $("#zip", root).classList.toggle("hidden", !c.ready);
  }

  function drawJobs() {
    const jobs = project.active_jobs.length ? project.active_jobs
      : (project.last_job && ["failed", "cancelled"].includes(project.last_job.status) && project.status !== "ready" ? [project.last_job] : []);
    $("#jobs", root).innerHTML = jobs.map((j) => jobPanel(j)).join("");
    $("#warnings", root).innerHTML = project.warnings.map((w) =>
      `<div class="alert warn mt">${icon.alert}<div class="grow">${esc(w)}</div></div>`).join("");
  }

  function drawInfo() {
    const gps = project.gameplay_media || [];
    $("#info", root).innerHTML = `
      <div class="card"><div class="card-head"><h3>Основное видео</h3></div>
        ${project.main_media ? `<div class="small muted" style="margin-bottom:8px">${esc(project.main_media.title)}</div>${mediaInfoHtml(project.main_media)}` : "—"}</div>
      <div class="card"><div class="card-head"><h3>Геймплей</h3><div class="spacer"></div><button class="btn sm ghost" data-act="settings">Изменить</button></div>
        ${gps.map((g) => `<div class="row" style="justify-content:space-between"><span>${esc(g.title)}</span><span class="muted small">${fmtDuration(g.info?.duration)}${g.status !== "ready" ? " · загружается" : ""}</span></div>`).join("") || '<span class="muted">Не выбран</span>'}
        <div class="small faint mt-s">${project.settings.main_audio ? "звук оригинала включён" : "без звука, только субтитры"} · ${project.settings.gameplay_audio ? "звук геймплея включён" : "звук геймплея выключен"} · ${project.settings.gameplay_mode === "random" ? "случайные фрагменты" : "последовательно"} · экран ${esc(project.settings.layout)}</div>
      </div>`;
  }

  function drawTimeline() {
    const eps = project.episodes;
    const total = project.timeline?.duration;
    $("#timeline", root).innerHTML = eps.length && total ? timelineHtml(eps, total)
      : `<div class="muted small">Границы частей появятся после анализа видео.</div>`;
    $("#tl-hint", root).textContent = eps.length ? "Цвет: зелёный — готово, жёлтый — создаётся, синий — нужно пересоздать; полоса снизу — одобрено" : "";
    $$("[data-seg]", root).forEach((s) => s.onclick = () => {
      const card = document.getElementById(`ep-${s.dataset.seg}`);
      card && card.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  function epKey(e) {
    return [e.status, e.rendered_at, e.approved, e.subtitle_status, e.error, e.publish_status].join("|");
  }

  function epCard(e) {
    let media;
    if (e.video_url) {
      media = `<video src="${esc(e.video_url)}" ${e.thumb_url ? `poster="${esc(e.thumb_url)}"` : ""} controls preload="none" playsinline></video>`;
    } else if (e.status === "rendering") {
      media = `<div class="placeholder"><div class="spinner"></div><div>Создаётся…</div></div>`;
    } else if (e.status === "error") {
      media = `<div class="placeholder" style="color:var(--err)">${icon.alert}<div>${esc(e.error || "Ошибка")}</div></div>`;
    } else {
      media = `<div class="placeholder">${icon.clock}<div>Ожидает обработки</div></div>`;
    }
    const canApprove = e.status === "ready";
    return `<div class="ep ${e.approved ? "approved" : ""}" id="ep-${esc(e.id)}">
      <div class="ep-video">${media}</div>
      <div class="ep-body">
        <div class="ep-meta"><span class="ep-num">Часть ${e.number}/${e.total}</span><span class="time" style="margin-left:6px">${fmtDuration(e.start_time)}–${fmtDuration(e.end_time)} · ${fmtSecondsShort(e.duration)}</span></div>
        <div class="ep-meta">${badge(EPISODE_STATUS, e.status)}${badge(SUB_STATUS, e.subtitle_status)}
          ${e.approved ? '<span class="badge ok">Одобрено</span>' : ""}</div>
        ${e.status === "error" && e.error ? `<div class="small" style="color:var(--err)">${esc(e.error)}</div>` : ""}
        <div class="ep-actions">
          <button class="btn sm" data-view="${esc(e.id)}" ${e.video_url ? "" : "disabled"}>${icon.play}Просмотр</button>
          <button class="btn sm" data-edit="${esc(e.id)}">${icon.edit}Редактировать</button>
          <button class="btn sm" data-render="${esc(e.id)}" ${e.status === "rendering" ? "disabled" : ""}>${icon.refresh}Пересоздать</button>
          <a class="btn sm ${e.download_url ? "" : "disabled"}" ${e.download_url ? `href="${esc(e.download_url)}"` : 'aria-disabled="true" style="pointer-events:none;opacity:.45"'}>${icon.download}Скачать</a>
          ${e.approved
            ? `<button class="btn sm ok wide" data-approve="${esc(e.id)}" data-v="0">${icon.check}Одобрено · отменить</button>`
            : `<button class="btn sm wide" data-approve="${esc(e.id)}" data-v="1" ${canApprove ? "" : "disabled"}>${icon.check}Одобрить</button>`}
        </div>
      </div></div>`;
  }

  function drawEpisodes() {
    const box = $("#episodes", root);
    const eps = project.episodes;
    const c = project.counts;
    $("#ep-hint", root).textContent = c.total ? `Готово ${c.ready} из ${c.total} · одобрено ${c.approved}` : "";
    if (!eps.length) {
      box.innerHTML = `<div class="card" style="grid-column:1/-1"><div class="muted">Части появятся здесь после анализа исходного видео.</div></div>`;
      cardKeys.clear();
      return;
    }
    const ids = new Set(eps.map((e) => e.id));
    for (const id of [...cardKeys.keys()]) {
      if (!ids.has(id)) { document.getElementById(`ep-${id}`)?.remove(); cardKeys.delete(id); }
    }
    if (box.querySelector(".card")) box.innerHTML = "";
    let prev = null;
    for (const e of eps) {
      const key = epKey(e);
      let node = document.getElementById(`ep-${e.id}`);
      if (!node || cardKeys.get(e.id) !== key) {
        const playing = node && [...node.querySelectorAll("video")].some((v) => !v.paused);
        if (!playing || !node) {
          const tmp = document.createElement("div");
          tmp.innerHTML = epCard(e);
          const fresh = tmp.firstElementChild;
          if (node) node.replaceWith(fresh); else if (prev) prev.after(fresh); else box.prepend(fresh);
          node = fresh;
          cardKeys.set(e.id, key);
        }
      }
      prev = node;
    }
  }

  function drawAll() { drawHead(); drawJobs(); drawInfo(); drawTimeline(); drawEpisodes(); }
  drawAll();

  async function reload() {
    project = await api.get(`/api/projects/${projectId}`);
    drawHead(); drawJobs(); drawTimeline(); drawEpisodes();
  }

  // ---------- actions ----------
  async function act(fn, okMsg) {
    try { await fn(); if (okMsg) toast(okMsg, "ok"); await reload(); } catch (e) { toastError(e); }
  }

  $("#build", root).onclick = () => act(() => api.post(`/api/projects/${projectId}/build`, {}), "Создание частей запущено");
  $("#more", root).onclick = (e) => { e.stopPropagation(); $("#menu", root).classList.toggle("hidden"); };
  const closeMenu = () => $("#menu", root)?.classList.add("hidden");
  document.addEventListener("click", closeMenu);

  root.addEventListener("click", async (e) => {
    const t = e.target.closest("button, a");
    if (!t) return;
    const d = t.dataset;
    if (d.cancel) return act(() => api.post(`/api/jobs/${d.cancel}/cancel`), "Отмена…");
    if (d.retry) return act(() => api.post(`/api/jobs/${d.retry}/retry`), "Задача запущена повторно");
    if (d.view) return viewEpisode(d.view);
    if (d.edit) { try { await openEditor(d.edit, reload); } catch (err) { toastError(err); } return; }
    if (d.render) return act(() => api.post(`/api/episodes/${d.render}/render`), "Часть поставлена на пересоздание");
    if (d.approve) return act(() => api.post(`/api/episodes/${d.approve}/approve`, { approved: d.v === "1" }));
    if (d.act) {
      closeMenu();
      switch (d.act) {
        case "approve-all":
          if (await confirmDialog("Одобрить все готовые части?", "Убедитесь, что вы просмотрели каждую часть. Одобренные части можно публиковать.", "Одобрить все"))
            act(() => api.post(`/api/projects/${projectId}/approve-all`), "Готовые части одобрены");
          break;
        case "rerender":
          if (await confirmDialog("Пересоздать все части?", "Все части будут отрисованы заново с текущими настройками. Распознанные субтитры и ваши правки сохранятся. Одобрение будет снято.", "Пересоздать"))
            act(() => api.post(`/api/projects/${projectId}/build`, { rerender_all: true }), "Пересоздание запущено");
          break;
        case "retranscribe":
          if (await confirmDialog("Распознать речь заново?", "Речь будет распознана повторно, а все ручные правки субтитров будут заменены. Затем части будут пересозданы.", "Распознать заново", true))
            act(() => api.post(`/api/projects/${projectId}/build`, { retranscribe: true, rerender_all: true }), "Распознавание запущено");
          break;
        case "captions":
          act(() => api.post(`/api/projects/${projectId}/captions/regenerate`), "Подписи и хештеги обновлены");
          break;
        case "settings":
          openSettings();
          break;
        case "delete":
          if (await confirmDialog("Удалить проект?", "Проект, все созданные части и субтитры будут удалены с диска. Исходные видео останутся в библиотеке.", "Удалить", true)) {
            try { await api.del(`/api/projects/${projectId}`); toast("Проект удалён", "ok"); location.hash = "#/projects"; }
            catch (err) { toastError(err); }
          }
          break;
      }
    }
  });

  function viewEpisode(id) {
    const e = project.episodes.find((x) => x.id === id);
    if (!e || !e.video_url) return;
    const m = modal({
      title: `Часть ${e.number}/${e.total}`,
      body: `<div class="player-modal"><video src="${esc(e.video_url)}" controls autoplay playsinline></video></div>
        ${e.caption ? `<div class="mt small muted">${esc(e.caption)} ${esc(e.hashtags)}</div>` : ""}`,
      footer: `<a class="btn" href="${esc(e.download_url)}">${icon.download}Скачать</a>
        ${e.status === "ready" ? (e.approved ? `<button class="btn ok" data-un>${icon.check}Одобрено · отменить</button>` : `<button class="btn primary" data-ok>${icon.check}Одобрить</button>`) : ""}`,
    });
    m.el.querySelector(".modal").style.maxWidth = "520px";
    const ok = m.el.querySelector("[data-ok]"), un = m.el.querySelector("[data-un]");
    if (ok) ok.onclick = async () => { m.close(); act(() => api.post(`/api/episodes/${id}/approve`, { approved: true }), "Часть одобрена"); };
    if (un) un.onclick = async () => { m.close(); act(() => api.post(`/api/episodes/${id}/approve`, { approved: false })); };
  }

  async function openSettings() {
    const library = await api.get("/api/media?kind=gameplay");
    const selected = new Set(project.gameplay_media_ids);
    const form = settingsForm(project.settings, { showCaption: true, showCrop: true, showAdvanced: true });
    const wrap = document.createElement("div");
    wrap.innerHTML = `<div class="field-label" style="margin-bottom:8px">Геймплей проекта</div>
      <div class="lib-list" style="margin-bottom:20px">${library.filter((g) => g.status === "ready").map((g) => `
        <div class="lib-item ${selected.has(g.id) ? "on" : ""}" data-gid="${esc(g.id)}"><div class="check">${icon.check}</div>
        <video src="${esc(g.file_url)}#t=1" preload="metadata" muted></video>
        <div class="grow"><div class="t">${esc(g.title)}</div><div class="small muted">${fmtDuration(g.info?.duration)}</div></div></div>`).join("")}</div>`;
    wrap.appendChild(form.el);
    $$("[data-gid]", wrap).forEach((it) => it.onclick = () => {
      const id = it.dataset.gid;
      selected.has(id) ? selected.delete(id) : selected.add(id);
      it.classList.toggle("on", selected.has(id));
    });
    const m = modal({
      title: "Настройки проекта", body: wrap, wide: true,
      footer: `<button class="btn" data-save>Сохранить</button><button class="btn primary" data-save-build>${icon.refresh}Сохранить и пересоздать</button>`,
    });
    m.el.querySelector(".modal").style.maxWidth = "900px";
    async function save(build) {
      const err = form.validate();
      if (err) return toastError(new Error(err));
      if (!selected.size) return toastError(new Error("Выберите хотя бы один клип геймплея."));
      const s = form.getValue();
      const durationChanged = Number(s.episode_seconds) !== Number(project.settings.episode_seconds)
        || Number(s.min_last_seconds) !== Number(project.settings.min_last_seconds);
      if (durationChanged && project.episodes.length && !(await confirmDialog("Изменить длительность частей?",
        "Серия будет разделена заново. Текущие части, правки субтитров и одобрения будут удалены.", "Изменить", true))) return;
      try {
        await api.patch(`/api/projects/${projectId}`, { settings: s, gameplay_media_ids: [...selected] });
        if (build) await api.post(`/api/projects/${projectId}/build`, { rerender_all: !durationChanged });
        toast(build ? "Настройки сохранены, части пересоздаются" : "Настройки сохранены. Изменённые части отмечены «Нужно пересоздать»", "ok");
        m.close();
        project = await api.get(`/api/projects/${projectId}`);
        drawAll();
      } catch (e) { toastError(e); }
    }
    m.el.querySelector("[data-save]").onclick = () => save(false);
    m.el.querySelector("[data-save-build]").onclick = () => save(true);
  }

  let stopped = false;
  async function loop() {
    if (stopped) return;
    try { await reload(); } catch { /* server restarting */ }
    setTimeout(loop, project.active_jobs.length ? 1500 : 5000);
  }
  setTimeout(loop, 1500);
  return () => { stopped = true; document.removeEventListener("click", closeMenu); };
}
