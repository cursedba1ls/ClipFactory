// Motion: page entrances, the sliding navigation highlight, number count-up, loading skeleton and a
// confetti burst for finished work. Everything is decorative: content is usable immediately, nothing waits
// for an animation, and the system "reduce motion" setting turns it all off.

export const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

const EASE = "cubic-bezier(.2,.8,.2,1)";

// Staggered entrance of a freshly rendered page. Only elements that exist right now are animated, so the
// periodic re-renders of a page (job progress, episode cards) never replay it.
export function animateIn(root) {
  if (reducedMotion() || !root?.animate) return;
  const blocks = [...root.children].filter((el) => !el.classList.contains("skeleton"));
  const items = [];
  for (const el of blocks) {
    const inner = el.matches(".grid, .stats, .projects, .episodes, .lib-list") ? [...el.children]
      : [...el.querySelectorAll(":scope > .grid > *, :scope > .stats > *, :scope .projects > *, :scope .episodes > *")];
    if (inner.length) items.push(...inner); else items.push(el);
    if (items.length >= 28) break;
  }
  items.slice(0, 28).forEach((el, i) => {
    el.animate(
      [{ opacity: 0, transform: "translateY(12px) scale(.985)" }, { opacity: 1, transform: "none" }],
      { duration: 420, delay: Math.min(i, 12) * 40, easing: EASE, fill: "backwards" },
    );
  });
}

// ---------------------------------------------------------------- navigation highlight
let pill = null;
export function moveNavPill(nav) {
  if (!nav) return;
  if (!pill) {
    pill = document.createElement("span");
    pill.className = "nav-pill";
    nav.prepend(pill);
    window.addEventListener("resize", () => moveNavPill(nav));
  }
  const active = nav.querySelector("a.active");
  if (!active) { pill.style.opacity = "0"; return; }
  const first = !pill.dataset.placed;
  if (first) pill.style.transition = "none";
  pill.style.opacity = "1";
  pill.style.transform = `translateY(${active.offsetTop}px)`;
  pill.style.height = `${active.offsetHeight}px`;
  pill.style.width = `${active.offsetWidth}px`;
  if (first) {
    pill.dataset.placed = "1";
    void pill.offsetWidth;  // apply the first position without animating from the top
    pill.style.transition = "";
  }
}

// ---------------------------------------------------------------- count-up numbers
const lastValues = new Map();
// Animates from the previously shown value (per key) to the new one; unchanged numbers stay still.
export function countUp(el, value, key) {
  const to = Number(value) || 0;
  const from = lastValues.has(key) ? lastValues.get(key) : 0;
  lastValues.set(key, to);
  if (reducedMotion() || from === to) { el.textContent = String(to); return; }
  const start = performance.now(), dur = Math.min(900, 380 + Math.abs(to - from) * 30);
  const step = (now) => {
    if (!el.isConnected) return;
    const t = Math.min(1, (now - start) / dur), k = 1 - (1 - t) ** 3;
    el.textContent = String(Math.round(from + (to - from) * k));
    if (t < 1) requestAnimationFrame(step);
  };
  el.textContent = String(from);
  requestAnimationFrame(step);
}

// ---------------------------------------------------------------- loading skeleton
export function skeleton() {
  return `<div class="skeleton" aria-hidden="true">
    <div class="sk sk-title"></div><div class="sk sk-sub"></div>
    <div class="sk-row"><div class="sk sk-card"></div><div class="sk sk-card"></div><div class="sk sk-card"></div></div>
    <div class="sk sk-wide"></div></div>`;
}

// ---------------------------------------------------------------- confetti
export function celebrate({ count = 110, duration = 1800 } = {}) {
  if (reducedMotion()) return;
  const canvas = document.createElement("canvas");
  canvas.className = "confetti";
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const W = window.innerWidth, H = window.innerHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  document.body.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const colors = ["#f04468", "#ffd400", "#3dd68c", "#5b9cff", "#ffffff", "#b06cff"];
  const parts = Array.from({ length: count }, (_, i) => {
    const fromLeft = i % 2 === 0;
    const angle = (fromLeft ? -60 : -120) * Math.PI / 180 + (Math.random() - 0.5) * 0.9;
    const speed = 9 + Math.random() * 9;
    return {
      x: fromLeft ? W * 0.08 : W * 0.92, y: H * 0.78,
      vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed,
      w: 6 + Math.random() * 6, h: 8 + Math.random() * 8, r: Math.random() * Math.PI,
      vr: (Math.random() - 0.5) * 0.35, c: colors[i % colors.length],
    };
  });
  const start = performance.now();
  const frame = (now) => {
    const t = now - start;
    ctx.clearRect(0, 0, W, H);
    const fade = Math.max(0, 1 - Math.max(0, t - duration * 0.6) / (duration * 0.4));
    for (const p of parts) {
      p.vy += 0.32; p.vx *= 0.99; p.vy *= 0.99;
      p.x += p.vx; p.y += p.vy; p.r += p.vr;
      ctx.save();
      ctx.globalAlpha = fade;
      ctx.translate(p.x, p.y); ctx.rotate(p.r);
      ctx.fillStyle = p.c;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h * Math.abs(Math.cos(p.r * 2)) + 1);
      ctx.restore();
    }
    if (t < duration) requestAnimationFrame(frame); else canvas.remove();
  };
  requestAnimationFrame(frame);
}
