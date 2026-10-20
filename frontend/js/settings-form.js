// Series settings form shared by "Новый проект", project settings and default settings.
import { esc, $, $$ } from "./ui.js";

export const PRESETS = [
  { id: "classic", name: "TikTok Classic", sample: `<span style="font-weight:800;color:#fff;-webkit-text-stroke:1.2px #000;paint-order:stroke fill;text-shadow:0 0 2px #000">Привет всем</span>` },
  { id: "karaoke", name: "Караоке", sample: `<span style="font-weight:800;color:#fff;-webkit-text-stroke:1.2px #000;paint-order:stroke fill">Привет <span style="color:#FFE600">всем</span></span>` },
  { id: "minimal", name: "Минимализм", sample: `<span style="font-weight:500;color:#fff;text-shadow:0 1px 3px #000">Привет всем</span>` },
  { id: "animated", name: "Анимация", sample: `<span style="font-weight:800;color:#fff;-webkit-text-stroke:1.2px #000;paint-order:stroke fill">Привет&nbsp;<span style="display:inline-block;transform:scale(1.2)">всем</span></span>` },
];

const DURATIONS = [30, 45, 60, 90];
const LABEL_FORMATS = ["Часть {n}/{total}", "Part {n}/{total}", "Серия {n}/{total}", "Часть {n}"];
const LABEL_POSITIONS = [
  ["top-center", "Сверху по центру"], ["top-left", "Сверху слева"], ["top-right", "Сверху справа"],
  ["bottom-center", "Снизу по центру"], ["bottom-left", "Снизу слева"], ["bottom-right", "Снизу справа"],
];

function seg(name, options, value) {
  return `<div class="seg" data-seg="${name}">${options.map(([v, label]) =>
    `<button type="button" data-v="${esc(v)}" class="${String(v) === String(value) ? "on" : ""}">${label}</button>`).join("")}</div>`;
}

function layoutIcon(top) {
  return `<span class="layout-opt"><i style="flex:${top}"></i><i style="flex:${100 - top}"></i></span>`;
}

export function settingsForm(initial, { showCaption = true, showCrop = false, showAdvanced = false } = {}) {
  const v = structuredClone(initial || {});
  v.subtitle_style = v.subtitle_style || { preset: "classic" };
  v.numbering = v.numbering || { enabled: true, format: "Часть {n}/{total}", position: "top-center", size: 54, margin: 70 };
  v.crop = v.crop || { main_x: 0.5, main_y: 0.5, game_x: 0.5, game_y: 0.5 };
  const custom = !DURATIONS.includes(Number(v.episode_seconds));
  const el = document.createElement("div");
  el.className = "stack";
  el.innerHTML = `
    <div class="form-grid">
      <div class="field full">
        <span class="field-label">Длительность одной части</span>
        <div class="row">
          ${seg("episode_seconds", [...DURATIONS.map((d) => [d, `${d} сек`]), ["custom", "Своя"]], custom ? "custom" : v.episode_seconds)}
          <span class="row ${custom ? "" : "hidden"}" data-custom-wrap>
            <input type="number" min="5" max="600" step="1" style="width:100px" data-custom value="${esc(v.episode_seconds ?? 60)}"> <span class="muted">сек</span>
          </span>
        </div>
      </div>
      <div class="field">
        <span class="field-label">Разделение экрана (мультфильм / геймплей)</span>
        ${seg("layout", [["50/50", `${layoutIcon(50)}50/50`], ["55/45", `${layoutIcon(55)}55/45`], ["60/40", `${layoutIcon(60)}60/40`]], v.layout || "50/50")}
      </div>
      <div class="field">
        <span class="field-label">Качество экспорта</span>
        ${seg("quality", [["fast", "Быстро"], ["balanced", "Баланс"], ["high", "Высокое"]], v.quality || "balanced")}
      </div>
      <div class="field full">
        <div class="row" style="justify-content:space-between">
          <span class="field-label">Стиль субтитров</span>
          <label class="switch"><input type="checkbox" data-k="subtitles_enabled" ${v.subtitles_enabled !== false ? "checked" : ""}><span class="track"></span><span class="small muted">Субтитры</span></label>
        </div>
        <div class="presets mt-s" data-presets>
          ${PRESETS.map((p) => `<div class="preset ${p.id === (v.subtitle_style.preset || "classic") ? "on" : ""}" data-preset="${p.id}">
            <div class="sample">${p.sample}</div><div class="name">${p.name}</div></div>`).join("")}
        </div>
      </div>
      <div class="field">
        <span class="field-label">Язык речи</span>
        ${seg("language", [["auto", "Авто"], ["ru", "Русский"], ["en", "English"]], v.language || "auto")}
      </div>
      <div class="field">
        <span class="field-label">Геймплей для частей</span>
        ${seg("gameplay_mode", [["continuous", "Последовательно"], ["random", "Случайный фрагмент"]], v.gameplay_mode || "continuous")}
      </div>
      <div class="field">
        <label class="switch"><input type="checkbox" data-k="numbering.enabled" ${v.numbering.enabled !== false ? "checked" : ""}><span class="track"></span><span>Нумерация частей</span></label>
        <div class="row mt-s" data-numbering-opts>
          <select data-k="numbering.format" style="flex:1;min-width:140px">${LABEL_FORMATS.map((f) =>
            `<option value="${esc(f)}" ${f === v.numbering.format ? "selected" : ""}>${esc(f.replace("{n}", "1").replace("{total}", "5"))}</option>`).join("")}
            ${LABEL_FORMATS.includes(v.numbering.format) ? "" : `<option value="${esc(v.numbering.format)}" selected>${esc(v.numbering.format)}</option>`}
          </select>
          <select data-k="numbering.position" style="flex:1;min-width:140px">${LABEL_POSITIONS.map(([p, l]) =>
            `<option value="${p}" ${p === (v.numbering.position || "top-center") ? "selected" : ""}>${l}</option>`).join("")}</select>
        </div>
      </div>
      <div class="field">
        <label class="switch"><input type="checkbox" data-k="gameplay_audio" ${v.gameplay_audio ? "checked" : ""}><span class="track"></span><span>Звук геймплея</span></label>
        <div class="slider-row mt-s ${v.gameplay_audio ? "" : "hidden"}" data-volume-wrap>
          <input type="range" min="0" max="1" step="0.05" data-k="gameplay_volume" value="${esc(v.gameplay_volume ?? 0.15)}">
          <output>${Math.round((v.gameplay_volume ?? 0.15) * 100)}%</output>
        </div>
        <div class="small faint mt-s ${v.gameplay_audio ? "hidden" : ""}" data-muted-note>По умолчанию звук геймплея выключен — звучит только оригинал.</div>
      </div>
      ${showCrop ? `
      <div class="field">
        <span class="field-label">Кадрирование мультфильма по горизонтали</span>
        <div class="slider-row"><input type="range" min="0" max="1" step="0.01" data-k="crop.main_x" value="${esc(v.crop.main_x)}"><output>${Math.round(v.crop.main_x * 100)}%</output></div>
      </div>
      <div class="field">
        <span class="field-label">Кадрирование геймплея по горизонтали</span>
        <div class="slider-row"><input type="range" min="0" max="1" step="0.01" data-k="crop.game_x" value="${esc(v.crop.game_x)}"><output>${Math.round(v.crop.game_x * 100)}%</output></div>
      </div>` : ""}
      ${showCaption ? `
      <label class="field"><span>Название для подписей (необязательно)</span>
        <input type="text" maxlength="100" data-k="caption_title" placeholder="Например: Том и Джерри" value="${esc(v.caption_title || "")}"></label>
      <label class="field"><span>Дополнительные хештеги</span>
        <input type="text" maxlength="500" data-k="hashtags" placeholder="#мультик #смешно" value="${esc(v.hashtags || "")}"></label>` : ""}
      ${showAdvanced ? `
      <label class="field"><span>Минимальная длина последней части, сек</span>
        <input type="number" min="0" max="60" step="1" data-k="min_last_seconds" value="${esc(v.min_last_seconds ?? 3)}">
        <span class="small faint">Более короткий остаток присоединяется к предыдущей части — ничего не теряется.</span></label>` : ""}
    </div>`;

  const emit = () => el.dispatchEvent(new CustomEvent("change-settings", { bubbles: true }));

  $$("[data-seg]", el).forEach((s) => s.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    $$("button", s).forEach((x) => x.classList.toggle("on", x === b));
    if (s.dataset.seg === "episode_seconds") {
      $("[data-custom-wrap]", el).classList.toggle("hidden", b.dataset.v !== "custom");
    }
    emit();
  }));
  $$("[data-preset]", el).forEach((p) => p.addEventListener("click", () => {
    $$("[data-preset]", el).forEach((x) => x.classList.toggle("on", x === p));
    emit();
  }));
  $$("input[type=range]", el).forEach((r) => r.addEventListener("input", () => {
    const out = r.parentElement.querySelector("output");
    if (out) out.textContent = `${Math.round(r.value * 100)}%`;
  }));
  const ga = $("[data-k=gameplay_audio]", el);
  ga.addEventListener("change", () => {
    $("[data-volume-wrap]", el).classList.toggle("hidden", !ga.checked);
    $("[data-muted-note]", el).classList.toggle("hidden", ga.checked);
  });
  const ne = $('[data-k="numbering.enabled"]', el);
  const syncNum = () => $$("[data-numbering-opts] select", el).forEach((s) => (s.disabled = !ne.checked));
  ne.addEventListener("change", syncNum);
  syncNum();
  $("[data-custom]", el).addEventListener("input", emit);

  function getValue() {
    const out = structuredClone(v);
    const segVal = (name) => $(`[data-seg="${name}"] button.on`, el)?.dataset.v;
    const d = segVal("episode_seconds");
    out.episode_seconds = d === "custom" ? Number($("[data-custom]", el).value) : Number(d);
    out.layout = segVal("layout");
    out.quality = segVal("quality");
    out.language = segVal("language");
    out.gameplay_mode = segVal("gameplay_mode");
    out.subtitles_enabled = $("[data-k=subtitles_enabled]", el).checked;
    const preset = $("[data-preset].on", el)?.dataset.preset || "classic";
    if (preset !== (v.subtitle_style.preset || "classic")) {
      out.subtitle_style = { preset, position: v.subtitle_style.position ?? null };
    } else {
      out.subtitle_style = { ...v.subtitle_style, preset };
    }
    out.numbering = { ...v.numbering, enabled: ne.checked,
      format: $('[data-k="numbering.format"]', el).value, position: $('[data-k="numbering.position"]', el).value };
    out.gameplay_audio = ga.checked;
    out.gameplay_volume = Number($("[data-k=gameplay_volume]", el).value);
    if (showCrop) {
      out.crop = { ...v.crop, main_x: Number($('[data-k="crop.main_x"]', el).value), game_x: Number($('[data-k="crop.game_x"]', el).value) };
    }
    if (showCaption) {
      out.caption_title = $("[data-k=caption_title]", el).value.trim();
      out.hashtags = $("[data-k=hashtags]", el).value.trim();
    }
    if (showAdvanced) out.min_last_seconds = Number($("[data-k=min_last_seconds]", el).value);
    return out;
  }

  function validate() {
    const s = getValue();
    if (!(s.episode_seconds >= 5 && s.episode_seconds <= 600)) return "Длительность части должна быть от 5 до 600 секунд.";
    return null;
  }

  return { el, getValue, validate };
}
