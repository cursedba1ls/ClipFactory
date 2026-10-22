// Visual crop editor: drag the frame over the cartoon / gameplay picture, zoom, or let it be automatic.
import { $, $$, esc, icon } from "./ui.js";

const OUT_W = 1080, OUT_H = 1920;
const STYLES = [
  ["dynamic", "Динамично"], ["follow", "Следить за действием"], ["static", "Одна рамка на часть"],
];
const STYLE_HINTS = {
  dynamic: "Рамка меняется на каждой смене сцены, плавно ведёт за героями и чередует общий, средний и крупный план",
  follow: "Рамка меняется на каждой смене сцены и плавно ведёт за героями, без приближения",
  static: "Одно положение рамки на всю часть — там, где больше всего действия",
};

// Same maths as backend video_composer.screen_regions: {main: [y, h], game: [y, h], banner: [y, h] | null}.
export function screenRegions(layout = "50/50", banner = null, gameplayTop = false, H = OUT_H) {
  const ratio = (Number(String(layout).split("/")[0]) || 50) / 100;
  const bh = banner && banner.enabled ? Math.floor(Math.max(60, Math.min(700, Number(banner.height) || 240)) / 2) * 2 : 0;
  const reserve = bh > 0 && (banner.mode || "reserve") === "reserve";
  const avail = H - (reserve ? bh : 0);
  const mainH = Math.round((avail * ratio) / 2) * 2, gameH = avail - mainH;
  const order = gameplayTop ? [["game", gameH], ["main", mainH]] : [["main", mainH], ["game", gameH]];
  if (reserve) order.splice({ top: 0, middle: 1, bottom: 2 }[banner.position] ?? 0, 0, ["banner", bh]);
  const out = { banner: null };
  let y = 0;
  for (const [name, h] of order) { out[name] = [y, h]; y += h; }
  if (bh && !reserve) {
    const pos = banner.position || "top";
    if (pos === "top") out.banner = [0, bh];
    else if (pos === "bottom") out.banner = [H - bh, bh];
    else {
      const split = out[gameplayTop ? "main" : "game"][0];
      out.banner = [Math.floor(Math.max(0, Math.min(H - bh, split - bh / 2)) / 2) * 2, bh];
    }
  }
  return out;
}

export function splitY(r) {
  return r.main[0] < r.game[0] ? r.main[0] + r.main[1] : r.main[0];
}

// Visible fraction of the picture (width, height) after cover-scaling + zoom — same maths as the renderer.
export function windowFractions(dims, targetW, targetH, zoom = 1) {
  const aSrc = dims.w / dims.h, aDst = targetW / targetH;
  const z = Math.max(1, zoom);
  return [Math.min(1, aDst / aSrc) / z, Math.min(1, aSrc / aDst) / z];
}

export function cropEditor({ main, game, layout = "50/50", crop = {}, auto = {}, divider = true, hint = "",
  banner = null, gameplayTop = false }) {
  const st = {
    mode: crop.mode || "auto", style: crop.style || "dynamic",
    main_x: crop.main_x ?? 0.5, main_y: crop.main_y ?? 0.5, main_zoom: crop.main_zoom ?? 1,
    game_x: crop.game_x ?? 0.5, game_y: crop.game_y ?? 0.5, game_zoom: crop.game_zoom ?? 1,
  };
  const autoVals = { main_x: auto.main_x ?? 0.5, main_y: auto.main_y ?? 0.5, game_x: auto.game_x ?? 0.5, game_y: auto.game_y ?? 0.5 };
  let lay = layout, ban = banner, gTop = gameplayTop;
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
    <div class="row" style="margin-bottom:12px" data-style-row>
      <span class="small"><b>Стиль автокадрирования:</b></span>
      <div class="seg" data-style>${STYLES.map(([v, l]) => `<button type="button" data-v="${v}">${l}</button>`).join("")}</div>
    </div>
    <div class="crop-grid">
      ${panel("main", "Мультфильм", main)}
      ${panel("game", "Геймплей", game)}
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
    const reg = screenRegions(lay, ban, gTop);
    const [wf, hf] = windowFractions(src.dims, OUT_W, reg[key][1], st[key + "_zoom"]);
    const v = vals(key);
    return { wf, hf, left: v.x * (1 - wf), top: v.y * (1 - hf) };
  }

  function draw() {
    $$("[data-mode] button", el).forEach((b) => b.classList.toggle("on", b.dataset.v === st.mode));
    $$("[data-style] button", el).forEach((b) => b.classList.toggle("on", b.dataset.v === st.style));
    $("[data-style-row]", el).classList.toggle("hidden", st.mode !== "auto");
    $("[data-mode-hint]", el).textContent = st.mode === "auto"
      ? STYLE_HINTS[st.style] || STYLE_HINTS.dynamic
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
    const W = canvas.width, H = canvas.height, k = H / OUT_H;
    const reg = screenRegions(lay, ban, gTop);
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, W, H);
    for (const key of ["main", "game"]) {
      const img = imgs[key], g = geometry(key);
      if (!img || !img.complete || !img.naturalWidth || !g) continue;
      const iw = img.naturalWidth, ih = img.naturalHeight;
      ctx.drawImage(img, g.left * iw, g.top * ih, g.wf * iw, g.hf * ih, 0, reg[key][0] * k, W, reg[key][1] * k);
    }
    const th = splitY(reg) * k;
    const touching = reg.main[0] + reg.main[1] === reg.game[0] || reg.game[0] + reg.game[1] === reg.main[0];
    if (divider && touching) { ctx.fillStyle = "#000"; ctx.fillRect(0, th - 1, W, 2); }
    if (reg.banner) {
      const [by, bh] = reg.banner;
      if ((ban.mode || "reserve") === "reserve") { ctx.fillStyle = ban.background || "#000"; ctx.fillRect(0, by * k, W, bh * k); }
      ctx.setLineDash([5, 4]); ctx.strokeStyle = "#ffd400"; ctx.lineWidth = 2;
      ctx.strokeRect(2, by * k + 2, W - 4, bh * k - 4); ctx.setLineDash([]);
      const bimg = bannerImg();
      if (bimg && bimg.complete && bimg.naturalWidth) {
        const s = Math.min(W / bimg.naturalWidth, (bh * k) / bimg.naturalHeight);
        const dw = bimg.naturalWidth * s, dh = bimg.naturalHeight * s;
        ctx.drawImage(bimg, (W - dw) / 2, by * k + (bh * k - dh) / 2, dw, dh);
      } else if (!ban.text) {
        ctx.fillStyle = "#ffd400"; ctx.font = "700 14px Arial"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText("место для баннера", W / 2, (by + bh / 2) * k);
      }
      if (ban.text) {
        ctx.font = `900 ${Math.max(8, (ban.text_size || 72) * k)}px 'Arial Black', Arial`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.lineWidth = 3; ctx.strokeStyle = "#000"; ctx.fillStyle = ban.text_color || "#fff";
        ctx.strokeText(ban.text, W / 2, (by + bh / 2) * k, W - 16); ctx.fillText(ban.text, W / 2, (by + bh / 2) * k, W - 16);
      }
    }
    ctx.font = "900 26px 'Arial Black', Arial, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.lineWidth = 6; ctx.strokeStyle = "#000"; ctx.fillStyle = "#fff"; ctx.lineJoin = "round";
    ctx.strokeText("субтитры", W / 2, th); ctx.fillText("субтитры", W / 2, th);
  }

  let bImg = null, bImgId = null;
  function bannerImg() {
    if (!ban || !ban.image_id) return null;
    if (bImgId !== ban.image_id) {
      bImgId = ban.image_id;
      bImg = new Image();
      bImg.onload = () => preview();
      bImg.src = `/api/banners/${ban.image_id}.png`;
    }
    return bImg;
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

  $("[data-style]", el).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    st.style = b.dataset.v;
    draw();
  });

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
    setScreen({ layout: l, banner: b, gameplay_top: t, divider: d }) {
      if (l) lay = l;
      ban = b || null; gTop = !!t;
      if (d !== undefined) divider = d;
      draw();
    },
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
