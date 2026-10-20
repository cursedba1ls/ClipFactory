import { api } from "../api.js";
import { settingsForm } from "../settings-form.js";
import { $, copyText, esc, icon, toast, toastError } from "../ui.js";

export async function render(root) {
  const [resp, sys, tiktok] = await Promise.all([
    api.get("/api/settings"), api.get("/api/system").catch(() => null), api.get("/api/publish/tiktok").catch(() => null),
  ]);
  const s = resp.settings;
  const meta = resp.meta;

  root.innerHTML = `
    <div class="page-head"><div><h1>Настройки</h1><div class="sub">Параметры по умолчанию, распознавание речи, рендер и публикация</div></div>
      <div class="spacer"></div><button class="btn primary" id="save">${icon.check}Сохранить настройки</button></div>
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
        </div></div>
    </div>
    <div class="card mt"><div class="card-head"><h2>Подписи</h2></div>
      <label class="field"><span>Хештеги по умолчанию (добавляются ко всем частям)</span>
        <input type="text" id="default_hashtags" maxlength="500" value="${esc(s.default_hashtags)}"></label></div>
    <div class="card mt" id="tiktok"></div>
    <div class="card mt"><div class="card-head"><h2>Система</h2></div><div id="sys"></div></div>`;

  const form = settingsForm(s.defaults, { showCaption: false, showAdvanced: true });
  $("#defaults", root).appendChild(form.el);

  $("#save", root).onclick = async () => {
    const err = form.validate();
    if (err) return toastError(new Error(err));
    const body = {
      ...s,
      defaults: { ...s.defaults, ...form.getValue() },
      whisper_model: $("#whisper_model", root).value,
      whisper_device: $("#whisper_device", root).value,
      encoder: $("#encoder", root).value,
      max_parallel_renders: Number($("#max_parallel_renders", root).value),
      max_download_mb: Number($("#max_download_mb", root).value),
      max_upload_mb: Number($("#max_upload_mb", root).value),
      force_normalize: $("#force_normalize", root).checked,
      default_hashtags: $("#default_hashtags", root).value,
    };
    try { await api.put("/api/settings", body); toast("Настройки сохранены", "ok"); } catch (e) { toastError(e); }
  };

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
      <dt>Папка данных</dt><dd><code>${esc(sys.storage_path)}</code></dd>
      <dt>Свободно на диске</dt><dd>${String(sys.disk_free_gb).replace(".", ",")} ГБ</dd>
    </dl>` : `<div class="muted">Нет данных</div>`;
}
