// In-app help: the essentials from README, always matching the installed version.
import { desktop, openFolder } from "../desktop.js";
import { esc, icon } from "../ui.js";

const STEPS = [
  ["Новый проект", "Добавьте основное видео (мультфильм) — ссылкой, файлом или через поиск на YouTube — и геймплей. Включите «Скачать только фрагмент → Под длину мультфильма», чтобы не качать часовую запись."],
  ["Настройки серии", "Длительность части, разделение экрана (40/60 … 70/30), геймплей сверху или снизу, стиль субтитров, место для баннера."],
  ["Кадрирование", "«Динамично» — рамка меняется на каждой сцене, ведёт за героями и чередует общий, средний и крупный план. «Вручную» — перетащите рамку мышью."],
  ["Создать видео", "Весь мультфильм делится на части без пропусков; субтитры распознаются из речи или берутся из файла .srt/.vtt."],
  ["Проверка и правки", "Откройте часть → «Редактировать»: текст и время фраз, стиль, кадр, геймплей, нумерация. «Сохранить и пересоздать» отрисует только эту часть."],
  ["Публикация", "«Очередь публикаций»: скачайте MP4, скопируйте подпись и хештеги или публикуйте через официальный TikTok API."],
];

const KEYS = [
  ["Пробел", "пуск / пауза видео в редакторе"],
  ["Ctrl + S", "сохранить правки части"],
  ["Ctrl + Enter", "сохранить и пересоздать часть"],
  ["Ctrl + Z / Ctrl + Y", "отменить / повторить правку субтитров"],
  ["⇤ / ⇥", "начало / конец фразы = текущее место плеера"],
];

export async function render(root) {
  root.innerHTML = `
    <div class="page-head"><div><h1>Справка</h1><div class="sub">ClipFactory ${esc(desktop.version || "")} — серии вертикальных видео из целых мультфильмов</div></div></div>
    <div class="grid grid-2">
      <div class="card"><div class="card-head"><h2>Как сделать серию</h2></div>
        <ol class="help-steps">${STEPS.map(([t, d]) => `<li><b>${esc(t)}</b><div class="small muted">${esc(d)}</div></li>`).join("")}</ol></div>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Клавиши в редакторе</h2></div>
          <dl class="kv">${KEYS.map(([k, d]) => `<dt><kbd>${esc(k)}</kbd></dt><dd>${esc(d)}</dd>`).join("")}</dl></div>
        <div class="card"><div class="card-head"><h2>Данные и обновления</h2></div>
          <div class="stack small">
            <div>Проекты, видео, черновики и настройки хранятся в отдельной папке и сохраняются при обновлении и переустановке. Правки сохраняются автоматически.</div>
            <div>Новые версии ClipFactory находит сам и предлагает «Перезапустить и обновить». Управление — «Настройки → Обновления».</div>
            ${desktop.desktop ? `<div class="row"><button class="btn sm" data-open="data">${icon.film}Папка данных</button><button class="btn sm ghost" data-open="logs">Журнал</button></div>` : ""}
          </div></div>
        <div class="card"><div class="card-head"><h2>Если что-то не работает</h2></div>
          <ul class="help-list small">
            <li>Обработка прервалась при закрытии — просто запустите ClipFactory снова: задачи продолжатся.</li>
            <li>Видео по ссылке не скачивается — загрузите его файлом (приватные и защищённые видео не скачиваются).</li>
            <li>Распознавание медленное — выберите модель «base» или «tiny» в настройках.</li>
            <li>Окно пустое — обновите Microsoft Edge WebView2 Runtime.</li>
            <li>Подробности ошибок — в журнале (кнопка выше) и в блоке «Технические подробности» у сообщения.</li>
          </ul>
          <div class="small mt-s"><a href="https://github.com/cursedba1ls/ClipFactory" target="_blank" rel="noopener">Страница проекта на GitHub</a> ·
            <a href="https://github.com/cursedba1ls/ClipFactory/releases" target="_blank" rel="noopener">Все версии</a></div></div>
      </div>
    </div>`;
  root.querySelectorAll("[data-open]").forEach((b) => (b.onclick = () => openFolder(b.dataset.open)));
}
