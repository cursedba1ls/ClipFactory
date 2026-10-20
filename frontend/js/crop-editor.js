// Visual crop editor: drag the frame over the cartoon / gameplay picture, zoom, or let it be automatic.
import { $, $$, esc, icon } from "./ui.js";

const OUT_W = 1080, OUT_H = 1920;
const TOP = { "50/50": 0.5, "55/45": 0.55, "60/40": 0.6 };

function topHeight(layout) {
  return Math.round((OUT_H * (TOP[layout] || 0.5)) / 2) * 2;
}

// Visible fraction of the picture (width, height) after cover-scaling + zoom — same maths as the renderer.
export function windowFractions(dims, targetW, targetH, zoom = 1) {
  const aSrc = dims.w / dims.h, aDst = targetW / targetH;
  const z = Math.max(1, zoom);
  return [Math.min(1, aDst / aSrc) / z, Math.min(1, aSrc / aDst) / z];
}

export function cropEditor({ main, game, layout = "50/50", crop = {}, auto = {}, divider = true, hint = "" }) {
  const st = {
    mode: crop.mode || "auto",
    main_x: crop.main_x ?? 0.5, main_y: crop.main_y ?? 0.5, main_zoom: crop.main_zoom ?? 1,
    game_x: crop.game_x ?? 0.5, game_y: crop.game_y ?? 0.5, game_zoom: crop.game_zoom ?? 1,
  };
  const autoVals = { main_x: auto.main_x ?? 0.5, main_y: auto.main_y ?? 0.5, game_x: auto.game_x ?? 0.5, game_y: auto.game_y ?? 0.5 };
  let lay = layout;
  const imgs = {};
  const el = document.createElement("div");
  el.className = "crop-ed";

  const panel = (key, label, src) => `
    <div class="crop-src" data-src="${key}">
      <div class="row" style="justify-content:space-between;margin-bottom:6px"><b class="small">${label}</b><span class="small faint" data-pos></span></div>
      ${src ? `<div class="crop-stage" data-stage><img src="${esc(src.url)}" alt="" draggable="false"><div class="crop-rect"></div></div>
        <div class="slider-row mt-s"><input type="range" min="1" max="3" step="0.05" data-zoom value="${st[key + "_zoom"]}"><output></output></div>
        <div class="small faint">Масштаб: увеличьте, чтобы приблизить</div>`
        : `<div class="crop-stage empty"><div class="small muted" style="padding:30px;text-align:center">Видео ещё не загружено</div></div>`}
    </div>`;

  el.innerHTML = `
    <div class="row" style="justify-content:space-between;margin-bottom:12px">
      <div class="seg" data-mode><button type="button" data-v="auto">${icon.refresh} Автоматически</button><button type="button" data-v="manual">Вручную</button></div>
      <span class="small muted" data-mode-hint></span>
    </div>
    <div class="crop-grid">
      ${panel("main", "Мультфильм (верх)", main)}
      ${panel("game", "Геймплей (низ)", game)}
      <div><div class="small" style="margin-bottom:6px"><b>Результат</b></div><canvas width="270" height="480" class="crop-canvas"></canvas></div>
    </div>
    ${hint ? `<div class="small faint mt-s">${esc(hint)}</div>` : ""}`;

  const canvas = $("canvas", el);

  function vals(key) {
    return st.mode === "auto" ? { x: autoVals[key + "_x"], y: autoVals[key + "_y"] } : { x: st[key + "_x"], y: st[key + "_y"] };
  }

  function geometry(key) {
    const src = key === "main" ? main : game;
    if (!src) return null;
    const th = topHeight(lay);
    const [wf, hf] = windowFractions(src.dims, OUT_W, key === "main" ? th : OUT_H - th, st[key + "_zoom"]);
    const v = vals(key);
    return { wf, hf, left: v.x * (1 - wf), top: v.y * (1 - hf) };
  }

  function draw() {
    $$("[data-mode] button", el).forEach((b) => b.classList.toggle("on", b.dataset.v === st.mode));
    $("[data-mode-hint]", el).textContent = st.mode === "auto"
      ? "Чёрные полосы убираются, рамка следует за действием в каждой части"
      : "Перетащите рамку мышью, колёсиком или ползунком измените масштаб";
    for (const key of ["main", "game"]) {
      const g = geometry(key);
      const box = $(`[data-src=${key}]`, el);
      if (!g) continue;
      const r = $(".crop-rect", box);
      Object.assign(r.style, { left: `${g.left * 100}%`, top: `${g.top * 100}%`, width: `${g.wf * 100}%`, height: `${g.hf * 100}%` });
      r.classList.toggle("auto", st.mode === "auto");
      const v = vals(key);
      $("[data-pos]", box).textContent = `${Math.round(v.x * 100)}% · ${Math.round(v.y * 100)}%`;
      $("output", box).textContent = `${st[key + "_zoom"].toFixed(2).replace(".", ",")}×`;
    }
    preview();
  }

  function preview() {
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height, th = Math.round(H * (TOP[lay] || 0.5));
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, W, H);
    for (const key of ["main", "game"]) {
      const img = imgs[key], g = geometry(key);
      if (!img || !img.complete || !img.naturalWidth || !g) continue;
      const iw = img.naturalWidth, ih = img.naturalHeight;
      const [dy, dh] = key === "main" ? [0, th] : [th, H - th];
      ctx.drawImage(img, g.left * iw, g.top * ih, g.wf * iw, g.hf * ih, 0, dy, W, dh);
    }
    if (divider) { ctx.fillStyle = "#000"; ctx.fillRect(0, th - 1, W, 2); }
    ctx.font = "900 26px 'Arial Black', Arial, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.lineWidth = 6; ctx.strokeStyle = "#000"; ctx.fillStyle = "#fff"; ctx.lineJoin = "round";
    ctx.strokeText("субтитры", W / 2, th); ctx.fillText("субтитры", W / 2, th);
  }

  function setManual() {
    if (st.mode === "auto") {
      st.main_x = autoVals.main_x; st.main_y = autoVals.main_y;
      st.game_x = autoVals.game_x; st.game_y = autoVals.game_y;
      st.mode = "manual";
    }
  }

  for (const key of ["main", "game"]) {
    const box = $(`[data-src=${key}]`, el);
    const stage = $("[data-stage]", box);
    if (!stage) continue;
    const img = $("img", stage);
    imgs[key] = img;
    img.onload = () => draw();
    let drag = null;
    const moveTo = (clientX, clientY, offset) => {
      const rect = stage.getBoundingClientRect();
      const g = geometry(key);
      let left = (clientX - rect.left) / rect.width - offset.x;
      let top = (clientY - rect.top) / rect.height - offset.y;
      left = Math.max(0, Math.min(1 - g.wf, left));
      top = Math.max(0, Math.min(1 - g.hf, top));
      st[key + "_x"] = g.wf < 0.999 ? left / (1 - g.wf) : 0.5;
      st[key + "_y"] = g.hf < 0.999 ? top / (1 - g.hf) : 0.5;
      draw();
    };
    stage.addEventListener("pointerdown", (e) => {
      setManual();
      const rect = stage.getBoundingClientRect();
      const g = geometry(key);
      const px = (e.clientX - rect.left) / rect.width, py = (e.clientY - rect.top) / rect.height;
      const inside = px >= g.left && px <= g.left + g.wf && py >= g.top && py <= g.top + g.hf;
      // Click outside the frame centres it there; inside — drag keeping the grab point.
      drag = inside ? { x: px - g.left, y: py - g.top } : { x: g.wf / 2, y: g.hf / 2 };
      stage.setPointerCapture(e.pointerId);
      moveTo(e.clientX, e.clientY, drag);
    });
    stage.addEventListener("pointermove", (e) => { if (drag) moveTo(e.clientX, e.clientY, drag); });
    stage.addEventListener("pointerup", () => { drag = null; });
    stage.addEventListener("wheel", (e) => {
      e.preventDefault();
      setManual();
      st[key + "_zoom"] = Math.max(1, Math.min(3, st[key + "_zoom"] - Math.sign(e.deltaY) * 0.1));
      $("[data-zoom]", box).value = st[key + "_zoom"];
      draw();
    }, { passive: false });
    $("[data-zoom]", box).addEventListener("input", (e) => { setManual(); st[key + "_zoom"] = Number(e.target.value); draw(); });
  }

  $("[data-mode]", el).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.v === "manual") setManual();
    else { st.mode = "auto"; st.main_zoom = 1; st.game_zoom = 1; $$("[data-zoom]", el).forEach((z) => (z.value = 1)); }
    draw();
  });

  draw();
  return {
    el,
    getValue: () => ({ ...st }),
    setLayout(l) { lay = l; draw(); },
    setDivider(d) { divider = d; draw(); },
  };
}

// Frame + picture size of a library media item for the editor (black bars already removed by the server).
export function sourceFromMedia(m, at = 0.33) {
  if (!m || m.status !== "ready" || !m.info) return null;
  const box = m.info.content_box;
  const t = Math.max(0, (m.info.duration || 0) * at);
  return { url: `/api/media/${m.id}/frame?t=${t.toFixed(1)}`, dims: box ? { w: box.w, h: box.h } : { w: m.info.width, h: m.info.height } };
}

export function focusOf(m) {
  return (m && m.info && m.info.focus) || { x: 0.5, y: 0.5 };
}
