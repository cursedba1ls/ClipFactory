import { api } from "../api.js";
import { badge, emptyState, esc, fmtDate, icon, jobPanel, PROJECT_STATUS, toastError, plural } from "../ui.js";

export function projectCard(p) {
  const c = p.counts;
  const job = p.active_jobs?.find((j) => j.status === "running") || p.active_jobs?.[0];
  return `<a class="pcard" href="#/project/${esc(p.id)}">
    <div class="thumb">${p.thumb_url ? `<img src="${esc(p.thumb_url)}" alt="" loading="lazy">` : icon.film}</div>
    <div class="info">
      <div class="title" title="${esc(p.title)}">${esc(p.title)}</div>
      <div class="row" style="gap:6px">${badge(PROJECT_STATUS, p.status)}
        ${c.total ? `<span class="small muted">${c.total} ${plural(c.total, "часть", "части", "частей")} · одобрено ${c.approved}</span>` : ""}</div>
      ${job ? `<div class="progress"><div style="width:${Math.round(job.progress)}%"></div></div>
        <div class="small muted">${esc(job.stage || "")} · ${Math.round(job.progress)}%</div>`
        : `<div class="small faint">${fmtDate(p.created_at)}</div>`}
    </div></a>`;
}

export async function render(root) {
  root.innerHTML = `
    <div class="page-head"><div><h1>Главная</h1><div class="sub">Создавайте серии вертикальных видео из целых мультфильмов</div></div>
      <div class="spacer"></div><a class="btn primary lg" href="#/new">${icon.plus}Новый проект</a></div>
    <div id="sys"></div>
    <div class="stats" id="stats"></div>
    <div id="reminders"></div>
    <div id="jobs"></div>
    <div class="card mt"><div class="card-head"><h2>Последние проекты</h2><div class="spacer"></div><a href="#/projects">Все проекты</a></div>
      <div id="recent"></div></div>`;

  async function load() {
    const d = await api.get("/api/dashboard");
    const s = d.stats;
    root.querySelector("#stats").innerHTML = [
      ["Проектов", s.projects], ["Создано частей", s.episodes], ["Ожидают проверки", s.pending, true],
      ["Одобрено", s.approved], ["Опубликовано", s.published],
    ].map(([l, v, a]) => `<div class="stat"><div class="label">${l}</div><div class="value ${a && v ? "accent" : ""}">${v}</div></div>`).join("");
    root.querySelector("#reminders").innerHTML = d.reminders.length ? `<div class="alert warn" style="margin-bottom:16px">${icon.clock}
      <div class="grow"><b>Пора публиковать</b>${d.reminders.map((r) =>
        `<div><a href="#/publish">«${esc(r.project_title)}» — часть ${r.number}</a>${r.approved ? "" : " · ещё не одобрена"}</div>`).join("")}</div></div>` : "";
    root.querySelector("#jobs").innerHTML = d.active_jobs.length
      ? `<div class="card-head"><h2>Сейчас выполняется</h2></div>${d.active_jobs.map((j) => jobPanel(j)).join("")}` : "";
    root.querySelector("#recent").innerHTML = d.recent_projects.length
      ? `<div class="projects">${d.recent_projects.map(projectCard).join("")}</div>`
      : emptyState("Пока нет проектов", "Добавьте мультфильм и геймплей — ClipFactory сам нарежет серию частей с субтитрами.",
        `<a class="btn primary" href="#/new">${icon.plus}Создать первый проект</a>`);
    return d.active_jobs.length;
  }

  async function loadSystem() {
    try {
      const s = await api.get("/api/system");
      const box = root.querySelector("#sys");
      const msgs = [];
      if (!s.ffmpeg.ffmpeg || !s.ffmpeg.ffprobe) {
        msgs.push(`<div class="alert err" style="margin-bottom:16px">${icon.alert}<div class="grow"><b>FFmpeg не найден.</b>
          Без него обработка видео невозможна. Переустановите ClipFactory поверх текущей версии — проекты и настройки сохранятся.</div></div>`);
      } else if (!s.ffmpeg.libass) {
        msgs.push(`<div class="alert warn" style="margin-bottom:16px">${icon.alert}<div class="grow">Ваша сборка FFmpeg без libass — субтитры не смогут быть вшиты в видео. Установите полную сборку FFmpeg (см. README).</div></div>`);
      }
      if (!s.whisper_installed) {
        msgs.push(`<div class="alert warn" style="margin-bottom:16px">${icon.alert}<div class="grow">Модуль распознавания речи не найден. Переустановите ClipFactory поверх текущей версии — данные сохранятся.</div></div>`);
      }
      box.innerHTML = msgs.join("");
    } catch { /* ignore */ }
  }

  root.addEventListener("click", async (e) => {
    const c = e.target.closest("[data-cancel]");
    if (c) { e.preventDefault(); try { await api.post(`/api/jobs/${c.dataset.cancel}/cancel`); load(); } catch (err) { toastError(err); } }
    const r = e.target.closest("[data-retry]");
    if (r) { e.preventDefault(); try { await api.post(`/api/jobs/${r.dataset.retry}/retry`); load(); } catch (err) { toastError(err); } }
  });

  await load();
  loadSystem();
  let timer = setInterval(async () => { try { await load(); } catch { /* ignore */ } }, 3000);
  return () => clearInterval(timer);
}
