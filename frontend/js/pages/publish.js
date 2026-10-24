import { api } from "../api.js";
import { releaseToTikTok } from "../tiktok-publish.js";
import { $, $$, badge, copyText, emptyState, esc, icon, modal, PUBLISH_STATUS, toast, toastError } from "../ui.js";

function toLocalInput(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export async function render(root) {
  let filter = "all";
  try { filter = localStorage.getItem("cf-publish-filter") || "all"; } catch { /* ignore */ }
  if (!["all", "unpublished", "published"].includes(filter)) filter = "all";
  let tiktok = null;
  let items = [];

  root.innerHTML = `
    <div class="page-head"><div><h1>Очередь публикаций</h1><div class="sub">Готовые части и их публикация в TikTok</div></div></div>
    <div class="card" id="mode"></div>
    <div class="row mt" style="justify-content:space-between">
      <div class="seg" id="filter">
        <button data-v="all">Все готовые</button><button data-v="unpublished">Не опубликованные</button><button data-v="published">Опубликованные</button>
      </div>
      <span class="muted small" id="q-count"></span>
    </div>
    <div class="card mt" style="padding:0" id="queue"></div>`;

  $$("#filter button", root).forEach((b) => {
    b.classList.toggle("on", b.dataset.v === filter);
    b.onclick = () => {
      filter = b.dataset.v;
      try { localStorage.setItem("cf-publish-filter", filter); } catch { /* ignore */ }
      $$("#filter button", root).forEach((x) => x.classList.toggle("on", x === b));
      drawQueue();
    };
  });

  async function loadTikTok() {
    try { tiktok = await api.get("/api/publish/tiktok"); } catch { tiktok = null; }
    const box = $("#mode", root);
    const connected = tiktok && tiktok.connected;
    box.innerHTML = `<div class="grid grid-2">
      <div class="stack" style="gap:6px"><h3>Режим A — ручная публикация</h3>
        <div class="muted small">Работает всегда и без доступа к API: скачайте MP4, скопируйте подпись и хештеги, откройте официальную страницу загрузки TikTok и отметьте часть как опубликованную.</div></div>
      <div class="stack" style="gap:6px"><h3>Режим B — официальный TikTok API ${connected ? '<span class="badge ok">подключён</span>' : '<span class="badge">не подключён</span>'}</h3>
        <div class="muted small">${connected
          ? `Аккаунт: <b>${esc(tiktok.user?.display_name || "подключён")}</b>. Кнопка «Выпустить в TikTok» загружает часть автоматически; статус подтверждается ответом TikTok.`
          : `Требуется собственное приложение в TikTok for Developers с доступом к Content Posting API. Пока приложение не прошло аудит TikTok, прямая публикация доступна только с приватностью «Только я». <a href="#/settings">Настроить</a>`}
        ${tiktok?.error ? `<div style="color:var(--err)">${esc(tiktok.error)}</div>` : ""}</div></div>
      </div>`;
  }

  async function loadQueue() {
    items = await api.get("/api/publish/queue");
    drawQueue();
  }

  function visible() {
    return items.filter((e) => {
      if (filter === "published") return ["published", "manual", "inbox"].includes(e.publish_status);
      if (filter === "unpublished") return !["published", "manual", "inbox"].includes(e.publish_status);
      return true;
    });
  }

  function drawQueue() {
    const list = visible();
    $("#q-count", root).textContent = list.length ? `${list.length} в очереди` : "";
    const box = $("#queue", root);
    if (!list.length) {
      box.innerHTML = emptyState("Очередь пуста", filter === "all"
        ? "Здесь появятся готовые части проектов."
        : "Нет частей, подходящих под фильтр.");
      return;
    }
    const connected = tiktok && tiktok.connected;
    box.innerHTML = list.map((e, i) => {
      const published = ["published", "manual"].includes(e.publish_status);
      const info = e.publish_info || {};
      return `<div class="q-item" data-id="${esc(e.id)}">
        <div class="q-thumb">${e.thumb_url ? `<img src="${esc(e.thumb_url)}" alt="">` : ""}</div>
        <div class="stack" style="gap:10px;min-width:0">
          <div class="row" style="gap:8px">
            <a href="#/project/${esc(e.project_id)}"><b>${esc(e.project_title)}</b></a><span class="muted">— часть ${e.number}/${e.total}</span>
            ${e.status === "outdated" ? '<span class="badge info">Нужно пересоздать</span>' : ""}
            ${badge(PUBLISH_STATUS, e.publish_status)}
            <div style="flex:1"></div>
            <button class="btn sm icon ghost" data-up ${i === 0 ? "disabled" : ""} title="Выше">${icon.up}</button>
            <button class="btn sm icon ghost" data-down ${i === list.length - 1 ? "disabled" : ""} title="Ниже">${icon.down}</button>
          </div>
          ${e.order_warning ? `<div class="alert warn small">${icon.alert}<div>${esc(e.order_warning)}</div></div>` : ""}
          ${info.reminder ? `<div class="alert warn small">${icon.clock}<div>${esc(info.reminder)}</div></div>` : ""}
          <div class="q-grid">
            <label class="field"><span>Подпись</span><textarea data-f="caption" maxlength="2200" rows="3">${esc(e.caption)}</textarea></label>
            <div class="stack" style="gap:10px">
              <label class="field"><span>Хештеги</span><input type="text" data-f="hashtags" maxlength="500" value="${esc(e.hashtags)}"></label>
              <div class="q-when">
                <label class="field"><span>Дата и время публикации</span><input type="datetime-local" data-f="scheduled_at" value="${toLocalInput(e.scheduled_at)}"></label>
                <label class="field"><span>Способ</span><select data-f="publish_mode">
                  <option value="manual" ${e.publish_mode === "manual" ? "selected" : ""}>Вручную</option>
                  <option value="api_direct" ${e.publish_mode === "api_direct" ? "selected" : ""} ${connected ? "" : "disabled"}>API: публикация</option>
                  <option value="api_inbox" ${e.publish_mode === "api_inbox" ? "selected" : ""} ${connected ? "" : "disabled"}>API: во «Входящие»</option>
                </select></label>
              </div>
            </div>
          </div>
          <div class="row" style="gap:6px">
            ${e.download_url ? `<a class="btn sm" href="${esc(e.download_url)}">${icon.download}Скачать MP4</a>` : ""}
            <button class="btn sm" data-copy-caption>${icon.copy}Копировать подпись</button>
            <button class="btn sm" data-copy-tags>${icon.copy}Копировать хештеги</button>
            <a class="btn sm" href="${esc(tiktok?.manual_upload_url || "https://www.tiktok.com/tiktokstudio/upload")}" target="_blank" rel="noopener">${icon.external}Открыть TikTok</a>
            ${published ? `<button class="btn sm ghost" data-unmark>Снять отметку</button>` : `<button class="btn sm" data-mark>${icon.check}Отметить опубликованным</button>`}
            ${e.status === "ready" && !published && e.publish_status !== "publishing" ? `<button class="btn sm primary" data-api>${icon.upload}Выпустить в TikTok</button>` : ""}
            ${info.publish_id && !["published", "failed"].includes(e.publish_status) ? `<button class="btn sm ghost" data-refresh>${icon.refresh}Обновить статус</button>` : ""}
          </div>
          ${info.message || info.error || info.manual_url ? `<div class="small muted">${esc(info.error || info.message || "")}
            ${info.manual_url ? `Ссылка: <a href="${esc(info.manual_url)}" target="_blank" rel="noopener">${esc(info.manual_url)}</a>` : ""}</div>` : ""}
        </div></div>`;
    }).join("");
    bind(list);
  }

  function bind(list) {
    $$(".q-item", root).forEach((row) => {
      const id = row.dataset.id;
      const e = items.find((x) => x.id === id);
      const patch = async (body, msg) => {
        try { const upd = await api.patch(`/api/episodes/${id}`, body); Object.assign(e, upd); if (msg) toast(msg, "ok", 2000); }
        catch (err) { toastError(err); }
      };
      $("[data-f=caption]", row).onchange = (ev) => patch({ caption: ev.target.value }, "Подпись сохранена");
      $("[data-f=hashtags]", row).onchange = (ev) => patch({ hashtags: ev.target.value }, "Хештеги сохранены");
      $("[data-f=scheduled_at]", row).onchange = async (ev) => {
        const v = ev.target.value;
        await patch(v ? { scheduled_at: new Date(v).getTime() / 1000 } : { clear_schedule: true }, v ? "Время публикации сохранено" : "Время публикации снято");
        loadQueue();
      };
      $("[data-f=publish_mode]", row).onchange = (ev) => patch({ publish_mode: ev.target.value }, "Способ публикации сохранён");
      $("[data-copy-caption]", row).onclick = () => copyText($("[data-f=caption]", row).value, "Подпись");
      $("[data-copy-tags]", row).onclick = () => copyText($("[data-f=hashtags]", row).value, "Хештеги");
      const mark = $("[data-mark]", row);
      if (mark) mark.onclick = () => markPublished(id);
      const unmark = $("[data-unmark]", row);
      if (unmark) unmark.onclick = async () => {
        try { await api.post(`/api/episodes/${id}/mark-published`, { published: false }); } catch (err) { toastError(err); }
        loadQueue().catch(toastError);
      };
      const apiBtn = $("[data-api]", row);
      if (apiBtn) apiBtn.onclick = () => releaseToTikTok(e, () => loadQueue().catch(() => {}));
      const ref = $("[data-refresh]", row);
      if (ref) ref.onclick = async () => { try { await api.post(`/api/publish/episodes/${id}/refresh`); loadQueue(); } catch (err) { toastError(err); } };
      const move = async (dir) => {
        const ids = list.map((x) => x.id);
        const i = ids.indexOf(id), j = i + dir;
        if (j < 0 || j >= ids.length) return;
        const a = list[i], b = list[j];
        try {
          if (a.scheduled_at && b.scheduled_at) {
            // Scheduled parts are ordered by their time: moving a part swaps the two publication times.
            await api.patch(`/api/episodes/${a.id}`, { scheduled_at: b.scheduled_at });
            await api.patch(`/api/episodes/${b.id}`, { scheduled_at: a.scheduled_at });
          } else if (!a.scheduled_at && !b.scheduled_at) {
            [ids[i], ids[j]] = [ids[j], ids[i]];
            await api.post("/api/publish/queue/order", { episode_ids: ids });
          } else {
            return toast("Запланированные части идут по времени публикации. Измените дату или снимите её.", "info");
          }
          await loadQueue();
        } catch (err) { toastError(err); }
      };
      $("[data-up]", row).onclick = () => move(-1);
      $("[data-down]", row).onclick = () => move(1);
    });
  }

  function markPublished(id) {
    const m = modal({
      title: "Отметить как опубликованную",
      body: `<label class="field"><span>Ссылка на видео в TikTok (необязательно)</span><input type="url" data-url placeholder="https://www.tiktok.com/@…/video/…"></label>`,
      footer: `<button class="btn" data-close2>Отмена</button><button class="btn primary" data-ok>${icon.check}Опубликовано</button>`,
    });
    m.el.querySelector("[data-close2]").onclick = m.close;
    m.el.querySelector("[data-ok]").onclick = async () => {
      try {
        await api.post(`/api/episodes/${id}/mark-published`, { url: m.el.querySelector("[data-url]").value, published: true });
        toast("Часть отмечена как опубликованная", "ok");
        m.close();
        loadQueue();
      } catch (e) { toastError(e); }
    };
  }

  await Promise.all([loadTikTok(), loadQueue()]);
  const timer = setInterval(() => {
    if (items.some((x) => x.publish_status === "publishing")) loadQueue().catch(() => {});
  }, 4000);
  return () => clearInterval(timer);
}
