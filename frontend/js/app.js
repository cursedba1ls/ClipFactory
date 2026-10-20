// Hash router. Each page exports render(root, params) and may return a cleanup function.
import { api } from "./api.js";
import { toast, toastError } from "./ui.js";
import * as dashboard from "./pages/dashboard.js";
import * as newProject from "./pages/new-project.js";
import * as projects from "./pages/projects.js";
import * as project from "./pages/project.js";
import * as gameplay from "./pages/gameplay.js";
import * as publish from "./pages/publish.js";
import * as settings from "./pages/settings.js";

const routes = [
  [/^\/?$/, "dashboard", dashboard],
  [/^\/new$/, "new", newProject],
  [/^\/projects$/, "projects", projects],
  [/^\/project\/([a-f0-9]+)$/, "projects", project],
  [/^\/gameplay$/, "gameplay", gameplay],
  [/^\/publish$/, "publish", publish],
  [/^\/settings$/, "settings", settings],
];

let cleanup = null;
const root = document.getElementById("app");

async function route() {
  const path = (location.hash || "#/").slice(1).split("?")[0];
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  root.querySelectorAll("video").forEach((v) => v.pause());
  const match = routes.find(([re]) => re.test(path)) || routes[0];
  const params = path.match(match[0])?.slice(1) || [];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === match[1]));
  root.innerHTML = "";
  window.scrollTo(0, 0);
  try {
    cleanup = (await match[2].render(root, params)) || null;
  } catch (e) {
    toastError(e);
    root.innerHTML = `<div class="alert err">${e.message || e}</div>`;
  }
}

window.addEventListener("hashchange", route);
route();

// Publication reminders (badge in the sidebar + one-time toast per episode).
const notified = new Set();
async function checkReminders() {
  try {
    const d = await api.get("/api/dashboard");
    const n = d.reminders.length;
    const b = document.getElementById("nav-reminders");
    b.textContent = n;
    b.classList.toggle("hidden", !n);
    for (const r of d.reminders) {
      if (notified.has(r.episode_id)) continue;
      notified.add(r.episode_id);
      toast(`Пора опубликовать: «${r.project_title}», часть ${r.number}${r.approved ? "" : " (ещё не одобрена)"}`, "warn", 9000);
    }
  } catch { /* server may be restarting */ }
}
checkReminders();
setInterval(checkReminders, 60000);
