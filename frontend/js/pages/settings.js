import { api } from "../api.js";
import { settingsForm } from "../settings-form.js";
import { $, copyText, esc, fmtDate, fmtSize, icon, toast, toastError } from "../ui.js";
import { debounce } from "../drafts.js";
import { desktop, installUpdate, openFolder, pollUpdates } from "../desktop.js";

export async function render(root) {
  const [resp, sys, tiktok] = await Promise.all([
    api.get("/api/settings"), api.get("/api/system").catch(() => null), api.get("/api/publish/tiktok").catch(() => null),
  ]);
  const s = resp.settings;
  const meta = resp.meta;

  root.innerHTML = `
    <div class="page-head"><div><h1>Настройки</h1><div class="sub">Параметры по умолчанию, распознавание речи, рендер, обновления и данные</div></div>
      <div class="spacer"></div><span class="save-state small muted" id="save-state">${icon.check} Изменения сохраняются автоматически</span></div>
    <div class="grid grid-2">
      <div class="card" id="updates"></div>
      <div class="card" id="data"></div>
    </div>
    <div class="mt"></div>
    <div class="card"><div class="card-head"><h2>Параметры новых проектов по умолчанию</h2></div><div id="defaults"></div></div>
    <div class="grid grid-2 mt">
      <div class="card"><div class="card-head"><h2>Распознавание речи</h2></div>
        <div class="stack">
          <label class="field"><span>Модель Whisper</span><select id="whisper_model">
            ${meta.whisper_models.map((m) => `<option value="${m.id}" ${m.id === s.whisper_model ? "selected" : ""}>${esc(m.name)}</option>`).join("")}
            ${meta.whisper_models.some((m) => m.id === s.whisper_model) ? "" : `<option value="${esc(s.whisper_model)}" selected>${esc(s.whisper_model)}</option>`}
          </select><span class="small faint">Модель скачивается один раз при первом распознавании (small ≈ 480 МБ) и дальше работает без интернета.
            ${sys && sys.whisper_models_downloaded.length ? `Уже скачаны: ${esc(sys.whisper_models_downloaded.join(", "))}.` : ""}</span></label>
          <label class="field"><span>Устройство</span><select id="whisper_device">
            <option value="auto" ${s.whisper_device === "auto" ? "selected" : ""}>Автоматически (видеокарта NVIDIA, если есть)</option>
            <option value="cpu" ${s.whisper_device === "cpu" ? "selected" : ""}>Только процессор</option>
            <option value="cuda" ${s.whisper_device === "cuda" ? "selected" : ""}>Видеокарта NVIDIA (CUDA)</option></select></label>
        </div></div>
      <div class="card"><div class="card-head"><h2>Рендер и загрузка</h2></div>
        <div class="stack">
          <label class="field"><span>Кодировщик видео</span><select id="encoder">
            <option value="auto" ${s.encoder === "auto" ? "selected" : ""}>Автоматически${sys && sys.hardware_encoders.length ? ` (найдено: ${sys.hardware_encoders.map((k) => meta.encoders[k]).join(", ")})` : " (аппаратный не найден — процессор)"}</option>
            ${Object.entries(meta.encoders).map(([k, v]) => `<option value="${k}" ${s.encoder === k ? "selected" : ""}>${esc(v)}</option>`).join("")}
          </select><span class="small faint">Если аппаратный кодировщик даст сбой, часть автоматически пересоздаётся на процессоре.</span></label>
          <label class="field"><span>Одновременно создаваемых частей</span><select id="max_parallel_renders">
            ${[1, 2, 3, 4].map((n) => `<option ${n === s.max_parallel_renders ? "selected" : ""}>${n}</option>`).join("")}</select>
            <span class="small faint">Для обычного ПК рекомендуется 1–2.</span></label>
          <div class="form-grid">
            <label class="field"><span>Лимит скачивания, МБ</span><input type="number" id="max_download_mb" min="10" value="${s.max_download_mb}"></label>
            <label class="field"><span>Лимит файла с ПК, МБ</span><input type="number" id="max_upload_mb" min="10" value="${s.max_upload_mb}"></label>
          </div>
          <label class="switch"><input type="checkbox" id="force_normalize" ${s.force_normalize ? "checked" : ""}><span class="track"></span><span class="small">Всегда выравнивать частоту кадров исходника (медленнее, для проблемных файлов)</span></label>
          <label class="switch"><input type="checkbox" id="keep_temp_files" ${s.keep_temp_files ? "checked" : ""}><span class="track"></span><span class="small">Сохранять временные файлы (для диагностики)</span></label>
        </div></div>
    </div>
    <div class="card mt"><div class="card-head"><h2>Подписи</h2></div>
      <label class="field"><span>Хештеги по умолчанию (добавляются ко всем частям)</span>
        <input type="text" id="default_hashtags" maxlength="500" value="${esc(s.default_hashtags)}"></label></div>
    <div class="card mt" id="tiktok"></div>
    <div class="card mt"><div class="card-head"><h2>Система</h2></div><div id="sys"></div></div>`;

  const form = settingsForm(s.defaults, { showCaption: false, showAdvanced: true });
  $("#defaults", root).appendChild(form.el);

  // ---- automatic saving: every change is written a moment after it is made
  const stateEl = $("#save-state", root);
  let current = s;
  function collect() {
    return {
      ...current,
      defaults: { ...current.defaults, ...form.getValue() },
      whisper_model: $("#whisper_model", root).value,
      whisper_device: $("#whisper_device", root).value,
      encoder: $("#encoder", root).value,
      max_parallel_renders: Number($("#max_parallel_renders", root).value),
      max_download_mb: Number($("#max_download_mb", root).value),
      max_upload_mb: Number($("#max_upload_mb", root).value),
      force_normalize: $("#force_normalize", root).checked,
      keep_temp_files: $("#keep_temp_files", root).checked,
      default_hashtags: $("#default_hashtags", root).value,
      update_check: $("#update_check", root)?.checked ?? current.update_check,
      update_auto_download: $("#update_auto_download", root)?.checked ?? current.update_auto_download,
    };
  }
  async function save() {
    const err = form.validate();
    if (err) { stateEl.innerHTML = `<span class="text-err">${esc(err)}</span>`; return; }
    stateEl.innerHTML = `<span class="spinner sm"></span> Сохранение…`;
    try {
      current = (await api.put("/api/settings", collect())).settings;
      stateEl.innerHTML = `${icon.check} Сохранено`;
    } catch (e) {
      stateEl.innerHTML = `<span class="text-err">${icon.alert} Не сохранено</span>`;
      toastError(e);
    }
  }
  const scheduleSave = debounce(save, 600);
  const onEdit = (e) => {
    if (e.target.closest("#tiktok") || e.target.closest("#data") || e.target.type === "file") return;
    if (!e.target.closest("#updates") || e.target.matches("#update_check, #update_auto_download")) scheduleSave();
  };
  root.addEventListener("input", onEdit);
  root.addEventListener("change", onEdit);
  form.el.addEventListener("change-settings", () => scheduleSave());
  window.addEventListener("pagehide", scheduleSave.flush);

  // ---- updates
  async function drawUpdates(st) {
    const box = $("#updates", root);
    if (!box) return;
    st = st || await api.get("/api/update").catch(() => null);
    if (!st) { box.innerHTML = `<div class="muted">Нет данных об обновлениях</div>`; return; }
    const stateText = {
      idle: st.last_check ? "Установлена последняя версия" : "Проверка ещё не выполнялась",
      checking: "Проверка…", available: `Доступна версия ${esc(st.available?.version || "")}`,
      downloading: `Загрузка ${Math.round(st.progress * 100)}%`, ready: `Версия ${esc(st.available?.version || "")} готова к установке`,
      installing: "Установка…", error: esc(st.error || "Ошибка"),
    }[st.state] || st.state;
    box.innerHTML = `<div class="card-head"><h2>Обновления</h2><div class="spacer"></div>
        <span class="badge ${st.state === "error" ? "err" : st.state === "ready" || st.state === "available" ? "warn" : "ok"}">${stateText}</span></div>
      <dl class="kv"><dt>Установленная версия</dt><dd><b>${esc(st.current_version)}</b>${st.channel && st.channel !== "stable" ? ` <span class="badge plain">${esc(st.channel)}</span>` : ""}</dd>
        <dt>Последняя проверка</dt><dd>${st.last_check ? fmtDate(st.last_check) : "—"}</dd></dl>
      ${st.installed_build ? `
      <div class="stack mt-s">
        <label class="switch"><input type="checkbox" id="update_check" ${current.update_check ? "checked" : ""}><span class="track"></span><span class="small">Проверять обновления автоматически</span></label>
        <label class="switch"><input type="checkbox" id="update_auto_download" ${current.update_auto_download ? "checked" : ""}><span class="track"></span><span class="small">Скачивать их в фоне (установка — только по вашей кнопке)</span></label>
      </div>
      <div class="row mt">
        <button class="btn" id="upd-check" ${st.state === "checking" || st.state === "downloading" ? "disabled" : ""}>${icon.refresh}Проверить обновления</button>
        ${st.state === "available" ? `<button class="btn primary" id="upd-dl">${icon.download}Скачать ${esc(st.available.version)}</button>` : ""}
        ${st.state === "ready" ? `<button class="btn primary" id="upd-install">${icon.refresh}Перезапустить и обновить</button>` : ""}
      </div>
      <div class="small faint mt-s">Обновления скачиваются с GitHub, проверяются по цифровой подписи и контрольной сумме. Проекты, видео и настройки при обновлении сохраняются.</div>`
      : `<div class="small faint mt-s">ClipFactory запущен из исходного кода — обновления устанавливаются только в установленной программе.</div>`}`;
    $("#upd-check", box)?.addEventListener("click", async () => {
      $("#upd-check", box).disabled = true;
      try { const r = await api.post("/api/update/check"); drawUpdates(r); pollUpdates(); if (r.state === "idle") toast("Установлена последняя версия", "ok"); } catch (e) { toastError(e); drawUpdates(); }
    });
    $("#upd-dl", box)?.addEventListener("click", async () => {
      try { drawUpdates(await api.post("/api/update/download")); pollUpdates(); } catch (e) { toastError(e); }
    });
    $("#upd-install", box)?.addEventListener("click", () => installUpdate(st));
    if (st.state === "downloading" || st.state === "checking") setTimeout(() => $("#updates", root) && drawUpdates(), 1500);
  }
  drawUpdates();

  // ---- data and backups
  async function drawData() {
    const box = $("#data", root);
    const b = await api.get("/api/backups").catch(() => ({ backups: [] }));
    const reason = (r) => r === "daily" ? "ежедневная" : r.startsWith("update") ? "перед обновлением" : r.startsWith("before-schema") ? "перед обновлением базы" : r === "manual" ? "вручную" : r;
    box.innerHTML = `<div class="card-head"><h2>Данные и резервные копии</h2></div>
      <div class="small muted">Проекты, видео, черновики и настройки хранятся отдельно от программы и не удаляются при обновлении.</div>
      <div class="input-row mt-s"><input type="text" readonly value="${esc(sys?.storage_path || desktop.data_dir || "")}">
        ${desktop.desktop ? `<button class="btn" id="open-data">Открыть папку</button>` : ""}</div>
      <div class="row mt" style="justify-content:space-between"><b class="small">Резервные копии базы (${b.backups.length})</b>
        <div class="row"><button class="btn sm" id="mk-backup">Создать копию сейчас</button>
        ${desktop.desktop ? `<button class="btn sm ghost" id="open-backups">Папка копий</button><button class="btn sm ghost" id="open-logs">Журнал</button>` : ""}</div></div>
      <div class="backup-list">${b.backups.slice(0, 6).map((x) => `<div class="row small"><span class="grow">${fmtDate(x.created_at)} · ${esc(reason(x.reason))}</span><span class="muted">${fmtSize(x.size)}</span></div>`).join("") || `<div class="small faint">Копий пока нет — первая создаётся автоматически при запуске.</div>`}</div>`;
    $("#open-data", box)?.addEventListener("click", () => openFolder("data"));
    $("#open-backups", box)?.addEventListener("click", () => openFolder("backups"));
    $("#open-logs", box)?.addEventListener("click", () => openFolder("logs"));
    $("#mk-backup", box).onclick = async () => {
      try { await api.post("/api/backups"); toast("Резервная копия создана", "ok"); drawData(); } catch (e) { toastError(e); }
    };
  }
  drawData();

  let connectTimer = null;
  function drawTikTok(t) {
    const box = $("#tiktok", root);
    if (!t) { box.innerHTML = `<div class="alert err">Не удалось получить состояние интеграции TikTok.</div>`; return; }
    box.innerHTML = `<div class="card-head"><h2>TikTok — официальный Content Posting API</h2><div class="spacer"></div>
        ${t.connected ? '<span class="badge ok">Аккаунт подключён</span>' : t.configured ? '<span class="badge warn">Не подключён</span>' : '<span class="badge">Не настроено</span>'}</div>
      <div class="alert info small" style="margin-bottom:16px">${icon.info}<div class="grow">
        Публикация через API требует вашего собственного приложения на <a href="https://developers.tiktok.com/" target="_blank" rel="noopener">developers.tiktok.com</a>
        с продуктами Login Kit и Content Posting API (права <code>video.upload</code>, <code>video.publish</code>).
        До прохождения аудита TikTok прямые публикации видны только вам («Только я»), поэтому для публичных постов используйте ручной режим или «Входящие».
        ClipFactory не обходит защиту TikTok и не использует cookies — только официальный OAuth. Ключ хранится в зашифрованном виде на этом компьютере.</div></div>
      <div class="form-grid">
        <label class="field"><span>Client Key</span><input type="text" id="tt-key" value="${esc(t.client_key)}" autocomplete="off"></label>
        <label class="field"><span>Client Secret ${t.has_secret ? "(сохранён)" : ""}</span><input type="password" id="tt-secret" placeholder="${t.has_secret ? "••••••••  — оставьте пустым, чтобы не менять" : ""}" autocomplete="new-password"></label>
        <label class="field full"><span>Redirect URI (укажите такой же в настройках приложения TikTok)</span>
          <div class="input-row"><input type="text" id="tt-redirect" value="${esc(t.redirect_uri || t.suggested_redirect_uri)}">
          <button class="btn" type="button" id="tt-copy">${icon.copy}</button></div></label>
      </div>
      <div class="row mt">
        <button class="btn" id="tt-save">Сохранить ключи</button>
        <a class="btn primary ${t.configured ? "" : "hidden"}" id="tt-connect" href="/api/publish/tiktok/connect" target="_blank" rel="noopener">${icon.link}${t.connected ? "Переподключить аккаунт" : "Подключить аккаунт TikTok"}</a>
        ${t.connected ? `<button class="btn danger" id="tt-disconnect">Отключить</button>` : ""}
        ${t.connected && t.user ? `<span class="muted small">Аккаунт: ${esc(t.user.display_name || "")}</span>` : ""}
      </div>
      ${t.error ? `<div class="alert err mt small">${icon.alert}<div>${esc(t.error)}</div></div>` : ""}`;
    $("#tt-copy", box).onclick = () => copyText($("#tt-redirect", box).value, "Адрес");
    $("#tt-save", box).onclick = async () => {
      try {
        const upd = await api.post("/api/publish/tiktok/config", {
          client_key: $("#tt-key", box).value, client_secret: $("#tt-secret", box).value, redirect_uri: $("#tt-redirect", box).value,
        });
        toast("Ключи TikTok сохранены", "ok");
        drawTikTok(upd);
      } catch (e) { toastError(e); }
    };
    // Authorisation happens in the browser: watch for the account to appear, then show it here.
    $("#tt-connect", box).onclick = () => {
      clearInterval(connectTimer);
      let left = 100;
      connectTimer = setInterval(async () => {
        if (!$("#tiktok", root) || --left < 0) return clearInterval(connectTimer);
        try {
          const st = await api.get("/api/publish/tiktok");
          if (st.connected && !t.connected) { clearInterval(connectTimer); drawTikTok(st); toast("Аккаунт TikTok подключён", "ok"); }
        } catch { /* keep waiting */ }
      }, 3000);
    };
    const dis = $("#tt-disconnect", box);
    if (dis) dis.onclick = async () => { try { drawTikTok(await api.post("/api/publish/tiktok/disconnect")); toast("Аккаунт отключён", "ok"); } catch (e) { toastError(e); } };
  }
  drawTikTok(tiktok);

  $("#sys", root).innerHTML = sys ? `<dl class="kv">
      <dt>Версия ClipFactory</dt><dd>${esc(sys.version)}</dd>
      <dt>FFmpeg</dt><dd>${sys.ffmpeg.ffmpeg ? esc(sys.ffmpeg.version || sys.ffmpeg.ffmpeg) : '<span class="badge err">не найден</span>'}</dd>
      <dt>Субтитры (libass)</dt><dd>${sys.ffmpeg.libass ? '<span class="badge ok">доступны</span>' : '<span class="badge err">нет</span>'}</dd>
      <dt>Распознавание речи</dt><dd>${sys.whisper_installed ? '<span class="badge ok">faster-whisper установлен</span>' : '<span class="badge err">не установлен</span>'}</dd>
      <dt>Аппаратное кодирование</dt><dd>${sys.hardware_encoders.length ? esc(sys.hardware_encoders.map((k) => meta.encoders[k]).join(", ")) : "не найдено — используется процессор"}</dd>
      <dt>Свободно на диске</dt><dd>${String(sys.disk_free_gb).replace(".", ",")} ГБ</dd>
    </dl>` : `<div class="muted">Нет данных</div>`;

  return () => {
    clearInterval(connectTimer);
    scheduleSave.flush();
    window.removeEventListener("pagehide", scheduleSave.flush);
  };
}
