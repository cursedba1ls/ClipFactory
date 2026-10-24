// "Автопубликация в TikTok" card on the project page: the series goes out part after part on a timetable.
import { api } from "./api.js";
import { $, badge, esc, PUBLISH_STATUS, toast, toastError } from "./ui.js";

const INTERVALS = [[30, "30 мин"], [60, "1 ч"], [120, "2 ч"], [180, "3 ч"], [360, "6 ч"], [720, "12 ч"], [1440, "24 ч"]];
const PRIVACY = {
  SELF_ONLY: "Только я", MUTUAL_FOLLOW_FRIENDS: "Друзья", FOLLOWER_OF_CREATOR: "Подписчики",
  PUBLIC_TO_EVERYONE: "Все",
};

function toLocalInput(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function when(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("ru-RU", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function autopublishCard(box, projectId) {
  let data = null;

  function form(s, tt) {
    const custom = !INTERVALS.some(([m]) => m === s.interval_minutes);
    const privacy = tt.privacy_options || Object.keys(PRIVACY);
    return `
      <div class="grid grid-3 mt">
        <label class="field"><span>Способ</span><select data-k="mode">
          <option value="direct" ${s.mode === "direct" ? "selected" : ""}>Сразу в профиль</option>
          <option value="inbox" ${s.mode === "inbox" ? "selected" : ""}>Во «Входящие» TikTok (дописать там)</option></select></label>
        <label class="field"><span>Кто видит</span><select data-k="privacy_level">${privacy.map((o) =>
          `<option value="${esc(o)}" ${s.privacy_level === o ? "selected" : ""}>${esc(PRIVACY[o] || o)}</option>`).join("")}</select></label>
        <label class="field"><span>Первая часть</span><input type="datetime-local" data-k="start_at" value="${toLocalInput(s.start_at)}" title="Пусто — как только часть готова"></label>
        <label class="field"><span>Интервал между частями</span><select data-k="interval">${INTERVALS.map(([m, l]) =>
          `<option value="${m}" ${s.interval_minutes === m ? "selected" : ""}>${l}</option>`).join("")}
          <option value="custom" ${custom ? "selected" : ""}>Своё…</option></select></label>
        <label class="field ${custom ? "" : "hidden"}" data-custom><span>Интервал, минут</span><input type="number" min="5" max="20160" data-k="interval_minutes" value="${s.interval_minutes}"></label>
        <label class="field"><span>Публиковать с — до</span><div class="row nowrap"><input type="time" data-k="window_start" value="${esc(s.window_start)}"><span class="muted">—</span><input type="time" data-k="window_end" value="${esc(s.window_end)}"></div></label>
        <label class="field"><span>Не больше частей в день</span><input type="number" min="0" max="50" data-k="max_per_day" value="${s.max_per_day}" title="0 — без ограничения"></label>
      </div>
      <div class="row mt">
        <label class="switch"><input type="checkbox" data-k="disable_comment" ${s.disable_comment ? "checked" : ""}><span class="track"></span><span class="small">Без комментариев</span></label>
        <label class="switch"><input type="checkbox" data-k="disable_duet" ${s.disable_duet ? "checked" : ""}><span class="track"></span><span class="small">Без дуэтов</span></label>
        <label class="switch"><input type="checkbox" data-k="disable_stitch" ${s.disable_stitch ? "checked" : ""}><span class="track"></span><span class="small">Без стежков</span></label>
        <div class="spacer"></div>
        <button class="btn primary" data-ap-save>${s.enabled ? "Сохранить и перестроить расписание" : "Включить автопубликацию"}</button>
      </div>
      <div class="small muted mt-s">Пустое «Первая часть» — как только часть готова. Время «с — до» одинаковое — круглые сутки.
        Ещё не созданная часть ждёт, следующие части — тоже: порядок серии не нарушается. Удалённые части пропускаются.</div>`;
  }

  function parts(list) {
    const auto = list.filter((p) => p.auto || ["published", "inbox", "publishing"].includes(p.publish_status));
    if (!auto.length) return "";
    const paused = list.find((p) => p.auto && p.publish_status === "failed");
    return `<div class="mt" style="border-top:1px solid var(--border);padding-top:12px">
      ${paused ? `<div class="row mt-s"><span class="small" style="color:var(--err)">Часть ${paused.number}: ${esc(paused.error || "не опубликовалась")}</span><div class="spacer"></div>
        <button class="btn sm" data-ap-retry>Повторить</button></div>` : ""}
      ${auto.map((p) => `<div class="row" style="padding:6px 0">
        <b style="min-width:72px">Часть ${p.number}</b><span class="muted small" style="min-width:150px">${esc(when(p.scheduled_at))}</span>
        ${badge(PUBLISH_STATUS, p.publish_status)}
        ${p.reminder && !["published", "manual", "inbox"].includes(p.publish_status) ? `<span class="small muted">${esc(p.reminder)}</span>` : ""}
      </div>`).join("")}</div>`;
  }

  function draw() {
    const s = data.settings, tt = data.tiktok;
    box.innerHTML = `
      <div class="card-head"><h2>Автопубликация в TikTok</h2><div class="spacer"></div>
        ${s.enabled ? `<button class="btn sm ghost" data-ap-off>Выключить</button>` : ""}
        ${s.enabled ? `<span class="badge ok">Включена</span>` : `<span class="badge plain">Выключена</span>`}</div>
      ${tt.connected ? form(s, tt) + parts(data.parts) : `<div class="row"><span class="muted">Чтобы части публиковались сами,
        подключите аккаунт TikTok.</span><div class="spacer"></div><a class="btn" href="#/settings">Подключить TikTok</a></div>`}`;
    const interval = $("[data-k=interval]", box);
    if (interval) interval.onchange = () => {
      const custom = interval.value === "custom";
      $("[data-custom]", box).classList.toggle("hidden", !custom);
      if (!custom) $("[data-k=interval_minutes]", box).value = interval.value;
    };
  }

  function value(enabled) {
    const v = (k) => $(`[data-k=${k}]`, box);
    const start = v("start_at").value;
    return {
      enabled, mode: v("mode").value, privacy_level: v("privacy_level").value,
      start_at: start ? new Date(start).getTime() / 1000 : null,
      interval_minutes: Number(v("interval_minutes").value), window_start: v("window_start").value || "00:00",
      window_end: v("window_end").value || "00:00", max_per_day: Number(v("max_per_day").value) || 0,
      require_approval: false, disable_comment: v("disable_comment").checked,
      disable_duet: v("disable_duet").checked, disable_stitch: v("disable_stitch").checked,
    };
  }

  async function save(body, msg) {
    try { data = await api.put(`/api/projects/${projectId}/autopublish`, body); draw(); toast(msg, "ok"); }
    catch (e) { toastError(e); }
  }

  box.addEventListener("click", (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.hasAttribute("data-ap-save")) {
      const body = value(true);
      if (!(body.interval_minutes >= 5)) return toastError(new Error("Интервал — не меньше 5 минут."));
      save(body, "Расписание сохранено");
    } else if (t.hasAttribute("data-ap-off")) {
      save({ ...data.settings, enabled: false }, "Автопубликация выключена");
    } else if (t.hasAttribute("data-ap-retry")) {
      save({ ...data.settings, enabled: true }, "Серия продолжится по расписанию");
    }
  });

  async function refresh() {
    if (box.contains(document.activeElement) && document.activeElement.matches("input, select")) return;
    try { data = await api.get(`/api/projects/${projectId}/autopublish`); draw(); } catch { /* server restarting */ }
  }
  refresh();
  return { refresh };
}
