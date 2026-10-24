// "Выпустить в TikTok" for one part. With a connected TikTok account the video is uploaded automatically through
// the official Content Posting API; without one the part goes out in three guided steps (save, copy, upload).
import { api } from "./api.js";
import { saveAs } from "./desktop.js";
import { $, $$, copyText, esc, icon, modal, toast, toastError } from "./ui.js";

const STUDIO = "https://www.tiktok.com/tiktokstudio/upload";

function captionOf(e) {
  return `${e.caption || ""} ${e.hashtags || ""}`.trim();
}

function fileName(e) {
  return `${(e.project_title || "ClipFactory").replace(/[\\/:*?"<>|]+/g, " ").slice(0, 80)} - Часть ${e.number}.mp4`;
}

// onDone() is called after something changed (publication started / part marked as published).
export async function releaseToTikTok(e, onDone) {
  if (e.publish_status === "publishing") return toast("Эта часть уже загружается в TikTok.", "info");
  if (e.status !== "ready" || !e.video_url) {
    return toastError(new Error(e.status === "outdated"
      ? "Часть изменена, но ещё не пересоздана. Нажмите «Пересоздать» и дождитесь готовности."
      : "Видео этой части ещё не готово."));
  }
  let tiktok = null;
  try { tiktok = await api.get("/api/publish/tiktok"); } catch { /* no status: offer manual upload */ }
  if (tiktok && tiktok.connected) {
    let creator = null;
    try { creator = await api.get("/api/publish/tiktok/creator-info"); } catch (err) {
      return manualDialog(e, onDone, `TikTok API сейчас недоступен: ${err.message}`);
    }
    return apiDialog(e, creator, onDone);
  }
  return manualDialog(e, onDone, null, tiktok);
}

function apiDialog(e, creator, onDone) {
  const opts = creator.privacy_level_options || ["SELF_ONLY"];
  const names = creator.privacy_names || {};
  const m = modal({
    title: `Выпустить в TikTok — часть ${e.number}/${e.total}`,
    body: `<div class="stack">
      <div class="seg" data-mode><button data-v="direct" class="on">Сразу в профиль</button><button data-v="inbox">Во «Входящие» TikTok</button></div>
      <div class="small muted" data-mode-note>Видео загрузится автоматически и появится в аккаунте ${esc(creator.creator_nickname || "")}.</div>
      <label class="field" data-privacy-wrap><span>Кто может смотреть</span><select data-privacy>${opts.map((o) =>
        `<option value="${esc(o)}" ${o === "PUBLIC_TO_EVERYONE" ? "selected" : ""}>${esc(names[o] || o)}</option>`).join("")}</select></label>
      <div class="row" data-toggles>
        <label class="switch"><input type="checkbox" data-nc ${creator.comment_disabled ? "checked disabled" : ""}><span class="track"></span><span class="small">Без комментариев</span></label>
        <label class="switch"><input type="checkbox" data-nd ${creator.duet_disabled ? "checked disabled" : ""}><span class="track"></span><span class="small">Без дуэтов</span></label>
        <label class="switch"><input type="checkbox" data-ns ${creator.stitch_disabled ? "checked disabled" : ""}><span class="track"></span><span class="small">Без стежков</span></label>
      </div>
      <div class="alert info small">${icon.info}<div>Подпись: ${esc(captionOf(e)) || "—"}</div></div>
    </div>`,
    footer: `<button class="btn" data-c>Отмена</button><button class="btn primary" data-go>${icon.upload}Выпустить</button>`,
  });
  let mode = "direct";
  $$("[data-mode] button", m.el).forEach((b) => b.onclick = () => {
    mode = b.dataset.v;
    $$("[data-mode] button", m.el).forEach((x) => x.classList.toggle("on", x === b));
    $("[data-privacy-wrap]", m.el).classList.toggle("hidden", mode !== "direct");
    $("[data-toggles]", m.el).classList.toggle("hidden", mode !== "direct");
    $("[data-mode-note]", m.el).textContent = mode === "direct"
      ? "Видео загрузится автоматически и сразу появится в профиле."
      : "Видео загрузится во «Входящие» приложения TikTok — там можно добавить музыку и нажать «Опубликовать».";
  });
  $("[data-c]", m.el).onclick = m.close;
  const go = $("[data-go]", m.el);
  go.onclick = async () => {
    go.disabled = true;
    try {
      await api.post(`/api/publish/episodes/${e.id}/tiktok`, {
        mode, privacy_level: $("[data-privacy]", m.el).value,
        disable_comment: $("[data-nc]", m.el).checked, disable_duet: $("[data-nd]", m.el).checked,
        disable_stitch: $("[data-ns]", m.el).checked,
      });
      toast(`Часть ${e.number} загружается в TikTok. Статус обновится после ответа TikTok.`, "ok");
      m.close();
      onDone && onDone();
    } catch (err) { go.disabled = false; toastError(err); }
  };
}

function manualDialog(e, onDone, problem = null, tiktok = null) {
  const configured = tiktok && tiktok.configured;
  const m = modal({
    title: `Выпустить в TikTok — часть ${e.number}/${e.total}`,
    body: `<div class="stack">
      ${problem ? `<div class="alert warn small">${icon.alert}<div>${esc(problem)}</div></div>`
        : `<div class="alert info small">${icon.info}<div>Чтобы части загружались в TikTok сами (кнопкой или по расписанию),
          один раз подключите аккаунт: «Настройки» → «TikTok»${configured ? " → «Подключить аккаунт TikTok»" : ""}.
          А пока — три шага вручную:</div></div>`}
      <div class="row nowrap"><b style="min-width:22px">1.</b><div class="grow">Сохраните видео на компьютер</div>
        <button class="btn sm" data-save>${icon.download}Сохранить видео</button></div>
      <div class="row nowrap"><b style="min-width:22px">2.</b><div class="grow">Скопируйте подпись и хештеги</div>
        <button class="btn sm" data-copy>${icon.copy}Копировать</button></div>
      <div class="row nowrap"><b style="min-width:22px">3.</b><div class="grow">Загрузите видео в TikTok Studio и вставьте подпись</div>
        <a class="btn sm" href="${esc(tiktok?.manual_upload_url || STUDIO)}" target="_blank" rel="noopener">${icon.external}Открыть TikTok</a></div>
      <label class="field"><span>Ссылка на видео в TikTok (необязательно)</span><input type="url" data-url placeholder="https://www.tiktok.com/@…/video/…"></label>
    </div>`,
    footer: `<a class="btn" href="#/settings" data-settings>${icon.link}Подключить TikTok</a>
      <button class="btn primary" data-done>${icon.check}Готово — опубликовано</button>`,
  });
  $("[data-save]", m.el).onclick = () => saveAs(e.download_url, fileName(e));
  $("[data-copy]", m.el).onclick = () => copyText(captionOf(e), "Подпись и хештеги");
  $("[data-settings]", m.el).onclick = () => m.close();
  const done = $("[data-done]", m.el);
  done.onclick = async () => {
    done.disabled = true;
    try {
      await api.post(`/api/episodes/${e.id}/mark-published`, { published: true, url: $("[data-url]", m.el).value });
      toast(`Часть ${e.number} отмечена как опубликованная`, "ok");
      m.close();
      onDone && onDone();
    } catch (err) { done.disabled = false; toastError(err); }
  };
}
