// Hash router. Each page exports render(root, params) and may return a cleanup function.
import { api } from "./api.js";
import { esc, toast, toastError } from "./ui.js";
import { initDesktop } from "./desktop.js";
import { animateIn, moveNavPill, skeleton } from "./motion.js";
import * as dashboard from "./pages/dashboard.js";
import * as newProject from "./pages/new-project.js";
import * as projects from "./pages/projects.js";
import * as project from "./pages/project.js";
import * as gameplay from "./pages/gameplay.js";
import * as publish from "./pages/publish.js";
import * as settings from "./pages/settings.js";
import * as help from "./pages/help.js";

const routes = [
  [/^\/?$/, "dashboard", dashboard],
  [/^\/new$/, "new", newProject],
  [/^\/projects$/, "projects", projects],
  [/^\/project\/([a-f0-9]+)$/, "projects", project],
  [/^\/gameplay$/, "gameplay", gameplay],
  [/^\/publish$/, "publish", publish],
  [/^\/settings$/, "settings", settings],
  [/^\/help$/, "help", help],
];

let cleanup = null;
let routeToken = 0;
let root = document.getElementById("app");

async function route() {
  const path = (location.hash || "#/").slice(1).split("?")[0];
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  root.querySelectorAll("video").forEach((v) => v.pause());
  // Every page gets a fresh container: listeners a page put on it (clicks, input) leave together with the page,
  // so a button is never handled by a page that is no longer shown (double dialogs, double requests).
  const fresh = root.cloneNode(false);
  root.replaceWith(fresh);
  root = fresh;
  const match = routes.find(([re]) => re.test(path)) || routes[0];
  const params = path.match(match[0])?.slice(1) || [];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === match[1]));
  moveNavPill(document.getElementById("nav"));
  const token = ++routeToken;
  const pageRoot = root;
  window.scrollTo(0, 0);
  // A page that takes a moment to load its data shows a skeleton instead of an empty screen.
  const skel = setTimeout(() => { if (token === routeToken && !root.childElementCount) root.innerHTML = skeleton(); }, 120);
  try {
    const done = (await match[2].render(pageRoot, params)) || null;
    clearTimeout(skel);
    if (token !== routeToken) { try { done && done(); } catch { /* ignore */ } return; }  // left while loading
    cleanup = done;
    pageRoot.querySelector(":scope > .skeleton")?.remove();
    animateIn(pageRoot);
  } catch (e) {
    clearTimeout(skel);
    if (token !== routeToken) return;
    toastError(e);
    pageRoot.innerHTML = `<div class="alert err">${esc(e.message || e)}</div>`;
  }
}

window.addEventListener("hashchange", route);
route();
initDesktop();

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
      toast(`Пора опубликовать: «${r.project_title}», часть ${r.number}`, "warn", 9000);
    }
  } catch { /* server may be restarting */ }
}
checkReminders();
setInterval(checkReminders, 60000);
