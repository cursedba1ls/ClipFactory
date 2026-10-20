import { api } from "../api.js";
import { emptyState, icon } from "../ui.js";
import { projectCard } from "./dashboard.js";

export async function render(root) {
  root.innerHTML = `<div class="page-head"><div><h1>Мои проекты</h1><div class="sub">Все созданные серии</div></div>
    <div class="spacer"></div><a class="btn primary" href="#/new">${icon.plus}Новый проект</a></div><div id="list"></div>`;
  async function load() {
    const list = await api.get("/api/projects");
    root.querySelector("#list").innerHTML = list.length
      ? `<div class="projects">${list.map(projectCard).join("")}</div>`
      : emptyState("Пока нет проектов", "Создайте первый проект — это займёт минуту.", `<a class="btn primary" href="#/new">${icon.plus}Новый проект</a>`);
    return list.some((p) => p.active_jobs.length);
  }
  await load();
  const timer = setInterval(() => load().catch(() => {}), 3000);
  return () => clearInterval(timer);
}
