// Episode editor modal: subtitle text/timing, style, crop, gameplay, numbering → save and re-render one part.
import { api } from "./api.js";
import { cropEditor } from "./crop-editor.js";
import { clearDraft, debounce, draftTime, loadDraft, saveDraft } from "./drafts.js";
import { PRESETS } from "./settings-form.js";
import { $, $$, esc, fmtDuration, icon, modal, toast, toastError } from "./ui.js";

const FONTS = ["Arial", "Arial Black", "Impact", "Segoe UI", "Verdana", "Tahoma", "Trebuchet MS", "Georgia",
  "Comic Sans MS", "Montserrat", "Roboto"];
const POSITIONS = [
  ["top-center", "Сверху по центру"], ["top-left", "Сверху слева"], ["top-right", "Сверху справа"],
  ["bottom-center", "Снизу по центру"], ["bottom-left", "Снизу слева"], ["bottom-right", "Снизу справа"],
];

const num = (v, d = 2) => (Math.round(Number(v) * 10 ** d) / 10 ** d).toFixed(d);

// Crop values compared on rounded numbers only, so a restored draft matches what the server has.
function normCrop(c = {}) {
  const r = (v, d) => Math.round((Number(v ?? d) + Number.EPSILON) * 1000) / 1000;
  return {
    mode: c.mode || "auto",
    main_x: r(c.main_x, 0.5), main_y: r(c.main_y, 0.5), main_zoom: r(c.main_zoom, 1),
    game_x: r(c.game_x, 0.5), game_y: r(c.game_y, 0.5), game_zoom: r(c.game_zoom, 1),
  };
}

export async function openEditor(episodeId, onSaved) {
  const [ep, library] = await Promise.all([api.get(`/api/episodes/${episodeId}`), api.get("/api/media?kind=gameplay")]);
  const eff = ep.effective;
  const st = { ...eff.subtitle_style };
  const nb = { ...eff.numbering };
  const crop = { main_x: 0.5, main_y: 0.5, game_x: 0.5, game_y: 0.5, ...eff.crop };
  let layout = eff.layout || "50/50";
  let cues = structuredClone(ep.cues || []);
  let touched = { cues: false, style: false, crop: false, numbering: false, gameplay: false };
  let cropDraft = null;   // crop values restored from a local draft
  let saved = false;      // after a successful save the draft is gone and must not come back
  let tab = "subs";
  const topPct = { "50/50": 50, "55/45": 55, "60/40": 60 };

  const body = document.createElement("div");
  body.className = "editor";
  body.innerHTML = `
    <div class="editor-left stack">
      ${ep.video_url ? `<video src="${esc(ep.video_url)}" controls preload="metadata" playsinline data-player></video>`
        : `<div class="ep-video" style="border-radius:12px;overflow:hidden"><div class="placeholder">${icon.film}<div>Видео ещё не создано</div></div></div>`}
      <div class="small muted">Часть ${ep.number} из ${ep.total} · ${fmtDuration(ep.start_time)}–${fmtDuration(ep.end_time)} исходника</div>
      ${ep.status === "outdated" ? `<div class="alert info small">${icon.info}<div>Показана предыдущая версия. Нажмите «Сохранить и пересоздать», чтобы применить изменения.</div></div>` : ""}
    </div>
    <div>
      <div class="tabs">
        <button data-tab="subs">${icon.text} Субтитры</button><button data-tab="style">Стиль</button>
        <button data-tab="crop">Кадрирование</button><button data-tab="game">Геймплей</button><button data-tab="num">Нумерация</button>
      </div>
      <div data-pane="subs"></div><div data-pane="style"></div><div data-pane="crop"></div>
      <div data-pane="game"></div><div data-pane="num"></div>
    </div>`;

  const m = modal({
    title: `Редактирование: часть ${ep.number}/${ep.total}`, body, wide: true,
    footer: `<button class="btn ghost" data-reset>Сбросить к настройкам проекта</button><div style="flex:1"></div>
      <button class="btn" data-save>Сохранить</button><button class="btn primary" data-save-render>${icon.refresh}Сохранить и пересоздать</button>`,
    onClose: () => {
      if (!saved && serialize() !== baseline) {
        toast("Несохранённые правки остались черновиком — откройте часть снова, и они появятся", "ok", 6000);
      }
    },
  });
  const player = $("[data-player]", body);

  // ---------- subtitles ----------
  function drawCues() {
    const pane = $('[data-pane="subs"]', body);
    pane.innerHTML = `
      ${ep.subtitle_status === "disabled" ? `<div class="alert warn small" style="margin-bottom:10px">${icon.info}<div>Субтитры выключены в настройках проекта.</div></div>` : ""}
      <div class="small muted" style="margin-bottom:10px">Время — в секундах от начала части (0–${num(ep.duration, 1)}). Нажмите ▶, чтобы перейти к фразе в плеере.</div>
      <div class="cue cue-head"><span>Начало</span><span>Конец</span><span>Текст</span><span></span><span></span></div>
      <div class="cues">${cues.map((c, i) => `
        <div class="cue" data-i="${i}">
          <input class="t" type="number" step="0.05" min="0" max="${ep.duration}" data-f="start" value="${num(c.start)}">
          <input class="t" type="number" step="0.05" min="0" max="${ep.duration}" data-f="end" value="${num(c.end)}">
          <input type="text" maxlength="300" data-f="text" value="${esc(c.text)}">
          <button class="btn sm icon" data-seek title="Перейти">${icon.play}</button>
          <button class="btn sm icon danger" data-del title="Удалить">${icon.trash}</button>
        </div>`).join("") || `<div class="muted small">В этой части нет распознанной речи. Можно добавить фразы вручную.</div>`}</div>
      <div class="row mt">
        <button class="btn sm" data-add>${icon.plus}Добавить фразу</button>
        <button class="btn sm" data-shift="-0.1">−0,1 с всем</button><button class="btn sm" data-shift="0.1">+0,1 с всем</button>
      </div>`;
    $$(".cue[data-i]", pane).forEach((row) => {
      const i = Number(row.dataset.i);
      row.querySelectorAll("input").forEach((inp) => inp.oninput = () => {
        touched.cues = true;
        cues[i][inp.dataset.f] = inp.dataset.f === "text" ? inp.value : Number(inp.value);
      });
      row.querySelector("[data-seek]").onclick = () => { if (player) { player.currentTime = cues[i].start; player.play(); } };
      row.querySelector("[data-del]").onclick = () => { touched.cues = true; cues.splice(i, 1); drawCues(); };
    });
    $("[data-add]", pane).onclick = () => {
      touched.cues = true;
      const last = cues[cues.length - 1];
      const start = player && !player.paused ? player.currentTime : (last ? Math.min(ep.duration - 0.5, last.end + 0.1) : 0);
      cues.push({ start: Number(num(start)), end: Number(num(Math.min(ep.duration, start + 1.5))), text: "Новая фраза", words: [] });
      cues.sort((a, b) => a.start - b.start);
      drawCues();
    };
    $$("[data-shift]", pane).forEach((b) => b.onclick = () => {
      touched.cues = true;
      const d = Number(b.dataset.shift);
      cues = cues.map((c) => ({ ...c, start: Math.max(0, c.start + d), end: Math.max(0.05, c.end + d),
        words: (c.words || []).map((w) => ({ ...w, start: Math.max(0, w.start + d), end: Math.max(0, w.end + d) })) }));
      drawCues();
    });
  }

  // ---------- style ----------
  function drawStyle() {
    const pane = $('[data-pane="style"]', body);
    const pos = st.position ?? topPct[layout];
    pane.innerHTML = `
      <div class="grid" style="grid-template-columns:minmax(0,1fr) 200px;gap:20px">
        <div class="stack">
          <div class="presets">${PRESETS.map((p) => `<div class="preset ${p.id === st.preset ? "on" : ""}" data-preset="${p.id}"><div class="sample">${p.sample}</div><div class="name">${p.name}</div></div>`).join("")}</div>
          <div class="form-grid">
            <label class="field"><span>Шрифт</span><select data-s="font_family">${[...new Set([st.font_family, ...FONTS])].map((f) => `<option ${f === st.font_family ? "selected" : ""}>${esc(f)}</option>`).join("")}</select></label>
            <label class="field"><span>Размер шрифта</span><div class="slider-row"><input type="range" min="40" max="160" data-s="font_size" value="${st.font_size}"><output>${st.font_size}</output></div></label>
            <div class="field"><span class="field-label">Цвета: текст · обводка · подсветка</span><div class="row">
              <input type="color" data-s="text_color" value="${st.text_color}" title="Цвет текста">
              <input type="color" data-s="outline_color" value="${st.outline_color}" title="Цвет обводки">
              <input type="color" data-s="highlight_color" value="${st.highlight_color}" title="Цвет подсветки слова (караоке)"></div></div>
            <label class="field"><span>Толщина обводки</span><div class="slider-row"><input type="range" min="0" max="14" step="0.5" data-s="outline" value="${st.outline}"><output>${st.outline}</output></div></label>
            <label class="field"><span>Вертикальное положение</span><div class="slider-row"><input type="range" min="5" max="95" data-s="position" value="${pos}"><output>${Math.round(pos)}%</output></div>
              <label class="switch small"><input type="checkbox" data-auto-pos ${st.position == null ? "checked" : ""}><span class="track"></span><span class="muted">На границе экранов (авто)</span></label></label>
            <label class="field"><span>Интенсивность анимации</span><div class="slider-row"><input type="range" min="0" max="1" step="0.05" data-s="animation" value="${st.animation}"><output>${Math.round(st.animation * 100)}%</output></div></label>
            <label class="field"><span>Слов во фразе (макс.)</span><div class="slider-row"><input type="range" min="2" max="6" step="1" data-s="max_words" value="${st.max_words}"><output>${st.max_words}</output></div>
              <span class="small faint">Применяется при повторном распознавании.</span></label>
            <div class="field"><span class="field-label">Начертание</span><div class="row">
              <label class="switch"><input type="checkbox" data-s="bold" ${st.bold ? "checked" : ""}><span class="track"></span><span class="small">Жирный</span></label>
              <label class="switch"><input type="checkbox" data-s="uppercase" ${st.uppercase ? "checked" : ""}><span class="track"></span><span class="small">ЗАГЛАВНЫЕ</span></label></div></div>
          </div>
        </div>
        <div><div class="field-label" style="margin-bottom:6px">Предпросмотр</div><div class="style-preview" data-preview></div>
          <div class="small faint mt-s">Приблизительный вид. Точный результат — после пересоздания.</div></div>
      </div>`;
    $$("[data-preset]", pane).forEach((p) => p.onclick = () => {
      const def = PRESETS.find((x) => x.id === p.dataset.preset);
      touched.style = true;
      st.preset = def.id;
      Object.assign(st, presetDefaults(def.id));
      drawStyle();
    });
    $$("[data-s]", pane).forEach((inp) => inp.oninput = inp.onchange = () => {
      touched.style = true;
      const k = inp.dataset.s;
      st[k] = inp.type === "checkbox" ? inp.checked : (inp.type === "range" ? Number(inp.value) : inp.value);
      if (k === "position") { $("[data-auto-pos]", pane).checked = false; }
      const out = inp.parentElement.querySelector("output");
      if (out) out.textContent = k === "animation" ? `${Math.round(st[k] * 100)}%` : k === "position" ? `${Math.round(st[k])}%` : st[k];
      drawPreview();
    });
    $("[data-auto-pos]", pane).onchange = (e) => {
      touched.style = true;
      if (e.target.checked) st.position = null; else st.position = Number($('[data-s="position"]', pane).value);
      drawPreview();
    };
    drawPreview();
  }

  function presetDefaults(id) {
    return {
      bigword: { font_family: "Arial Black", font_size: 124, bold: true, outline: 10, shadow: 0, animation: 0.25, max_words: 2 },
      classic: { font_family: "Arial", font_size: 82, bold: true, outline: 7, shadow: 0, animation: 0, max_words: 4 },
      karaoke: { font_family: "Arial", font_size: 82, bold: true, outline: 7, shadow: 0, animation: 0.3, max_words: 4 },
      minimal: { font_family: "Arial", font_size: 64, bold: false, outline: 2.5, shadow: 1, animation: 0, max_words: 4 },
      animated: { font_family: "Arial", font_size: 84, bold: true, outline: 7, shadow: 0, animation: 0.6, max_words: 4 },
    }[id];
  }

  function drawPreview() {
    const box = $("[data-preview]", body);
    if (!box) return;
    const w = box.clientWidth || 200, scale = w / 1080;
    const top = topPct[layout];
    const pos = st.position ?? top;
    const text = (cues[0]?.text || "Пример субтитров").split(" ").slice(0, 5).join(" ");
    const shown = st.uppercase ? text.toUpperCase() : text;
    const words = shown.split(" ");
    const html = st.preset === "karaoke" && words.length > 1
      ? words.map((wd, i) => i === 1 ? `<span style="color:${st.highlight_color}">${esc(wd)}</span>` : esc(wd)).join(" ")
      : esc(shown);
    const thumb = ep.thumb_url ? `url('${ep.thumb_url}')` : "none";
    const ol = Math.max(0, st.outline * scale);
    box.innerHTML = `
      <div class="top" style="height:${top}%;background-image:${thumb};background-size:cover;background-position:top center"></div>
      <div class="bottom" style="height:${100 - top}%;background-image:${thumb};background-size:cover;background-position:bottom center"></div>
      <div class="sub" style="top:${pos}%;font-family:'${esc(st.font_family)}',Arial;font-size:${st.font_size * scale}px;font-weight:${st.bold ? 800 : 500};
        color:${st.text_color};-webkit-text-stroke:${ol * 2}px ${st.outline_color};paint-order:stroke fill">${html}</div>
      ${nb.enabled ? `<div class="lbl" style="${nb.position.startsWith("top") ? `top:${(nb.margin / 1920) * 100}%;transform:none` : `bottom:${(nb.margin / 1920) * 100}%;top:auto;transform:none`};
        text-align:${nb.position.endsWith("left") ? "left" : nb.position.endsWith("right") ? "right" : "center"};
        font-family:Arial;font-weight:800;font-size:${nb.size * scale}px;color:#fff;-webkit-text-stroke:${Math.max(1, (nb.size / 14) * scale * 2)}px #000;paint-order:stroke fill">${esc(nb.format.replace("{n}", ep.number).replace("{total}", ep.total))}</div>` : ""}`;
  }

  // ---------- crop ----------
  let cropEd = null;
  let cropStart = null;
  function drawCrop() {
    const pane = $('[data-pane="crop"]', body);
    const src = ep.crop_sources || {};
    pane.innerHTML = `
      <div class="field" style="margin-bottom:14px"><span class="field-label">Разделение экрана</span>
        <div class="seg" data-layout>${["50/50", "55/45", "60/40"].map((l) => `<button type="button" data-v="${l}" class="${l === layout ? "on" : ""}">${l}</button>`).join("")}</div></div>
      <div data-crop-host></div>`;
    const r = eff.crop_resolved || {};
    cropEd = cropEditor({
      main: src.main, game: src.game, layout,
      crop: cropDraft || { ...r, mode: crop.mode || "auto" },
      auto: { main_x: src.main?.auto?.x, main_y: src.main?.auto?.y, game_x: src.game?.auto?.x, game_y: src.game?.auto?.y },
      hint: "Видео масштабируется без искажений. Рамка показывает, что попадёт в кадр этой части.",
    });
    $("[data-crop-host]", pane).appendChild(cropEd.el);
    cropStart = JSON.stringify(normCrop({ ...r, mode: crop.mode || "auto" }));
    $("[data-layout]", pane).onclick = (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      touched.crop = true;
      layout = b.dataset.v;
      $$("[data-layout] button", pane).forEach((x) => x.classList.toggle("on", x === b));
      cropEd.setLayout(layout);
    };
  }

  // ---------- gameplay ----------
  let gp = { media_id: ep.gameplay?.media_id || "", offset: ep.gameplay?.offset ?? 0, random: false };
  function drawGame() {
    const pane = $('[data-pane="game"]', body);
    const ready = library.filter((x) => x.status === "ready");
    pane.innerHTML = ready.length ? `
      <div class="lib-list" style="max-height:330px">${ready.map((x) => `
        <div class="lib-item ${x.id === gp.media_id ? "on" : ""}" data-gid="${esc(x.id)}"><div class="check">${icon.check}</div>
          <video src="${esc(x.file_url)}#t=1" preload="metadata" muted></video>
          <div class="grow"><div class="t">${esc(x.title)}</div><div class="small muted">${fmtDuration(x.info?.duration)}</div></div></div>`).join("")}</div>
      <div class="form-grid mt">
        <label class="field"><span>Начать геймплей с секунды</span><input type="number" min="0" step="0.5" data-offset value="${num(gp.offset, 1)}"></label>
        <div class="field"><span class="field-label">&nbsp;</span><label class="switch"><input type="checkbox" data-rand><span class="track"></span><span>Случайный фрагмент</span></label></div>
      </div>
      <div class="small faint mt-s">Замена геймплея не требует повторного распознавания речи — пересоздаётся только эта часть.</div>`
      : `<div class="muted">Библиотека геймплея пуста. Добавьте клипы на странице «Геймплей».</div>`;
    $$("[data-gid]", pane).forEach((it) => it.onclick = () => {
      touched.gameplay = true;
      gp.media_id = it.dataset.gid;
      $$("[data-gid]", pane).forEach((x) => x.classList.toggle("on", x === it));
    });
    const off = $("[data-offset]", pane), rnd = $("[data-rand]", pane);
    if (off) off.oninput = () => { touched.gameplay = true; gp.offset = Number(off.value); };
    if (rnd) rnd.onchange = () => { touched.gameplay = true; gp.random = rnd.checked; off.disabled = rnd.checked; };
  }

  // ---------- numbering ----------
  function drawNum() {
    const pane = $('[data-pane="num"]', body);
    pane.innerHTML = `<div class="form-grid">
      <div class="field full"><label class="switch"><input type="checkbox" data-n="enabled" ${nb.enabled ? "checked" : ""}><span class="track"></span><span>Показывать номер части</span></label></div>
      <label class="field"><span>Текст (используйте {n} — номер, {total} — всего частей)</span><input type="text" maxlength="60" data-n="format" value="${esc(nb.format)}"></label>
      <label class="field"><span>Положение</span><select data-n="position">${POSITIONS.map(([v, l]) => `<option value="${v}" ${v === nb.position ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label class="field"><span>Размер</span><div class="slider-row"><input type="range" min="24" max="110" data-n="size" value="${nb.size}"><output>${nb.size}</output></div></label>
      <label class="field"><span>Отступ от края</span><div class="slider-row"><input type="range" min="20" max="300" data-n="margin" value="${nb.margin}"><output>${nb.margin}</output></div></label>
    </div><div class="small faint mt">Если номер пересекается с субтитрами, он автоматически переносится к противоположному краю.</div>`;
    $$("[data-n]", pane).forEach((inp) => inp.oninput = inp.onchange = () => {
      touched.numbering = true;
      const k = inp.dataset.n;
      nb[k] = inp.type === "checkbox" ? inp.checked : inp.type === "range" ? Number(inp.value) : inp.value;
      const out = inp.parentElement.querySelector("output");
      if (out) out.textContent = nb[k];
    });
  }

  function showTab(t) {
    tab = t;
    $$(".tabs button", body).forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    $$("[data-pane]", body).forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== t));
    if (t === "style") drawPreview();
  }
  $$(".tabs button", body).forEach((b) => b.onclick = () => showTab(b.dataset.tab));
  drawCues(); drawStyle(); drawCrop(); drawGame(); drawNum();
  showTab("subs");

  // ---------- local drafts: unsaved edits survive closing the window or a reload ----------
  const draftKey = `episode:${episodeId}`;
  const initial = {
    cues: structuredClone(cues), st: { ...st }, nb: { ...nb }, layout, gp: { ...gp },
    crop: normCrop(cropEd ? cropEd.getValue() : {}),
  };
  function serialize() {
    return JSON.stringify({ cues, st, nb, layout, gp, crop: normCrop(cropEd ? cropEd.getValue() : cropDraft || {}) });
  }
  const baseline = serialize();
  const persistDraft = () => {
    if (saved) return;
    const state = serialize();
    if (state === baseline) clearDraft(draftKey);
    else saveDraft(draftKey, JSON.parse(state), baseline);
  };
  const scheduleDraft = debounce(persistDraft);
  for (const type of ["input", "change", "click", "pointerup"]) body.addEventListener(type, scheduleDraft);

  function applyDraft(s) {
    cues = structuredClone(s.cues);
    Object.assign(st, s.st);
    Object.assign(nb, s.nb);
    layout = s.layout;
    gp = { ...s.gp };
    cropDraft = s.crop;
    touched = {
      cues: JSON.stringify(cues) !== JSON.stringify(initial.cues),
      style: JSON.stringify(st) !== JSON.stringify(initial.st),
      crop: JSON.stringify(normCrop(cropDraft)) !== JSON.stringify(initial.crop) || layout !== initial.layout,
      numbering: JSON.stringify(nb) !== JSON.stringify(initial.nb),
      gameplay: gp.media_id !== initial.gp.media_id || Number(gp.offset) !== Number(initial.gp.offset) || !!gp.random,
    };
    drawCues(); drawStyle(); drawCrop(); drawGame(); drawNum();
  }

  const draft = loadDraft(draftKey);
  if (draft && JSON.stringify(draft.state) !== baseline) {
    const stale = draft.base !== baseline;
    const banner = document.createElement("div");
    banner.className = `alert ${stale ? "warn" : "info"}`;
    banner.innerHTML = `${icon.info}<div class="grow">Есть несохранённый черновик от ${esc(draftTime(draft))}.
      ${stale ? "Часть уже сохранена с другими настройками: восстановление заменит их." : "Восстановить правки?"}</div>
      <button class="btn sm primary" type="button" data-restore>Восстановить</button>
      <button class="btn sm ghost" type="button" data-discard>Отбросить</button>`;
    body.before(banner);
    banner.querySelector("[data-restore]").onclick = () => { applyDraft(draft.state); banner.remove(); toast("Черновик восстановлен", "ok", 2500); };
    banner.querySelector("[data-discard]").onclick = () => { clearDraft(draftKey); banner.remove(); };
  } else if (draft) {
    clearDraft(draftKey);
  }

  async function save(render) {
    const payload = { render };
    if (touched.cues) {
      for (const c of cues) {
        if (!(c.end > c.start)) return toastError(new Error("У каждой фразы конец должен быть позже начала."));
      }
      payload.cues = cues.map((c) => ({ start: Number(c.start), end: Number(c.end), text: c.text, words: c.words || [] }));
    }
    const overrides = {};
    if (touched.style) {
      overrides.subtitle_style = {
        preset: st.preset, font_family: st.font_family, font_size: st.font_size, bold: st.bold,
        text_color: st.text_color, outline_color: st.outline_color, outline: st.outline, shadow: st.shadow,
        highlight_color: st.highlight_color, position: st.position, animation: st.animation,
        uppercase: st.uppercase, max_words: st.max_words,
      };
    }
    const cropNow = cropEd ? cropEd.getValue() : null;
    if (cropNow && JSON.stringify(normCrop(cropNow)) !== cropStart) { overrides.crop = cropNow; overrides.layout = layout; }
    else if (touched.crop) { overrides.layout = layout; }
    if (touched.numbering) {
      if (!nb.format.includes("{n}")) return toastError(new Error("Текст нумерации должен содержать {n}."));
      overrides.numbering = nb;
    }
    if (Object.keys(overrides).length) payload.overrides = overrides;
    if (touched.gameplay && gp.media_id) {
      payload.gameplay = { media_id: gp.media_id, offset: gp.random ? null : gp.offset, random_offset: gp.random };
    }
    try {
      await api.patch(`/api/episodes/${episodeId}`, payload);
      saved = true;
      clearDraft(draftKey);
      toast(render ? "Изменения сохранены, часть пересоздаётся" : "Изменения сохранены", "ok");
      m.close();
      onSaved && onSaved();
    } catch (e) { toastError(e); }
  }
  $("[data-save]", m.el).onclick = () => save(false);
  $("[data-save-render]", m.el).onclick = () => save(true);
  $("[data-reset]", m.el).onclick = async () => {
    try {
      await api.patch(`/api/episodes/${episodeId}`, { reset_overrides: true });
      saved = true;
      clearDraft(draftKey);
      toast("Индивидуальные настройки части сброшены", "ok");
      m.close();
      onSaved && onSaved();
    } catch (e) { toastError(e); }
  };
}
