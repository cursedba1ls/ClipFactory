# Выпуск новой версии ClipFactory

Установленные копии ClipFactory сами находят, скачивают и устанавливают новые версии с GitHub Releases.
Выпуск полностью собирается и проверяется в GitHub Actions на чистой Windows-машине. Вручную нужно только
поднять номер версии и отправить тег.

## Как выпустить версию (коротко)

1. Внесите изменения и закоммитьте их.
2. Поднимите версию в `backend/version.py` (`VERSION = "2.1.0"`): исправления → третья цифра, новые
   возможности → вторая, несовместимые изменения → первая. Бета-версии: `2.1.0-beta.1`. Они публикуются
   как pre-release и установленным копиям **не** предлагаются.
3. Добавьте в `CHANGELOG.md` раздел `## 2.1.0` с описанием изменений. Этот текст пользователи увидят
   в окне «Что нового».
4. Закоммитьте и отправьте тег:
   ```
   git commit -am "Release 2.1.0"
   git tag v2.1.0
   git push origin HEAD --tags
   ```
5. Workflow **Windows** (`.github/workflows/windows.yml`):
   - прогоняет все тесты на Windows;
   - собирает программу (PyInstaller) и установщик (Inno Setup);
   - на чистой Windows устанавливает её, запускает окно, создаёт серию видео, обновляет на следующую
     версию через встроенный механизм обновлений, проверяет сохранность всех данных и удаляет программу
     (`packaging/tests/e2e_installed.py`);
   - подписывает `latest.json` ключом из секрета `CLIPFACTORY_UPDATE_KEY` и публикует GitHub Release
     с `ClipFactory-Setup-2.1.0.exe`, `.sha256` и `latest.json`.
6. В течение 6 часов (или сразу, если пользователь нажмёт «Проверить обновления») установленные копии
   скачают версию в фоне и предложат «Перезапустить и обновить».

Если тесты или проверка установки не прошли, релиз не публикуется. Исправьте ошибку, удалите тег
(`git push --delete origin v2.1.0 && git tag -d v2.1.0`) и повторите с тем же номером.

## Как устроены обновления

| Шаг | Где | Что проверяется |
|---|---|---|
| Проверка | `backend/services/updater.py` | `latest.json` скачивается только по HTTPS с github.com. Подпись Ed25519 проверяется открытым ключом из `backend/update_keys.py`. Версия должна быть новее установленной. |
| Загрузка | там же | Установщик скачивается в `%LOCALAPPDATA%\ClipFactory\updates`. Размер и SHA-256 сверяются с подписанным манифестом, несовпадающий файл удаляется. Перед запуском хэш проверяется ещё раз. |
| Установка | `backend/desktop.py`, `packaging/installer.iss` | Создаётся резервная копия базы. Окно закрывается, установщик запускается с `/VERYSILENT /UPDATE`, ждёт выхода программы (мьютекс `ClipFactoryAppMutex`) и заменяет только файлы программы. Затем он запускает новую версию, а она подтверждает обновление и показывает «Что нового». |
| Сбой | | Если установщик не завершился, остаётся старая версия: Inno Setup откатывает изменения, программа сообщает об ошибке, и скачанный файл можно установить ещё раз. |

Данные пользователя (`%LOCALAPPDATA%\ClipFactory`: база, проекты, видео, черновики, настройки, модели)
лежат отдельно от программы (`%LOCALAPPDATA%\Programs\ClipFactory`). Установщик их никогда не трогает.
Деинсталлятор удаляет их, только если пользователь сам выбрал «Да».

### Правила совместимости данных

- База SQLite обновляется только **добавлением**: новые таблицы (`CREATE TABLE IF NOT EXISTS`) и новые
  столбцы (список `MIGRATIONS` в `backend/database/db.py`). При изменении схемы увеличьте `SCHEMA_VERSION`:
  перед миграцией автоматически делается резервная копия.
- Никогда не удаляйте и не переименовывайте столбцы и ключи JSON, которые читают старые версии.
- Новые настройки добавляйте с значениями по умолчанию в `backend/models/schemas.py`. Неизвестные или
  ставшие недопустимыми значения при чтении заменяются умолчаниями поштучно (`settings_store.lenient`),
  остальные настройки сохраняются.

## Ключ подписи обновлений (один раз)

Манифест подписывается закрытым ключом Ed25519. Открытая часть уже прописана в `backend/update_keys.py`.

1. GitHub → репозиторий → **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `CLIPFACTORY_UPDATE_KEY`, Secret: содержимое файла `CLIPFACTORY_UPDATE_KEY.txt`
   (одна строка base64).
3. Храните копию ключа в надёжном месте (менеджер паролей). Без секрета выпуск всё равно публикуется,
   но без `latest.json`: установленные копии его не увидят.

**Если ключ утёк или потерян:** создайте новую пару:

```
python -c "import base64;from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K;from cryptography.hazmat.primitives import serialization as s;k=K.generate();print('private', base64.b64encode(k.private_bytes(s.Encoding.Raw,s.PrivateFormat.Raw,s.NoEncryption())).decode());print('public ', base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw,s.PublicFormat.Raw)).decode())"
```

Добавьте новый открытый ключ в `PUBLIC_KEYS` и выпустите версию, ещё подписанную **старым** ключом.
После этого замените секрет новым ключом. Старый открытый ключ уберите через несколько версий.
Если старый ключ потерян, пользователям нужно один раз установить новую версию вручную.

## Сборка установщика вручную (Windows)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-build.txt
choco install innosetup        (или установщик с jrsoftware.org)
.venv\Scripts\python packaging\build_windows.py
```

Результат: `dist\ClipFactory-Setup-<версия>.exe` и `.sha256`. FFmpeg скачивается автоматически
(`packaging/ffmpeg.json`, проверка SHA-256). Чтобы обновить FFmpeg, поменяйте `version`, `urls` и `sha256`.

Подписать манифест локально:
`set CLIPFACTORY_UPDATE_KEY=...` и `python packaging\make_manifest.py dist\ClipFactory-Setup-2.1.0.exe`.

## Структура проекта

```
backend/
  version.py            номер версии (единственный источник)
  build_info.py         сведения о сборке (build.json внутри установленной программы)
  desktop.py            окно программы (WebView2), один экземпляр, запуск установщика обновлений
  update_keys.py        открытые ключи подписи обновлений
  main.py, api/, services/, jobs/, database/, models/   сервер и логика (см. README)
  services/updater.py   проверка, загрузка, проверка подписи и установка обновлений
  services/backup.py    резервные копии базы
frontend/               интерфейс (HTML/CSS/JS без сборки)
packaging/
  build_windows.py      сборка: build.json → FFmpeg → PyInstaller → Inno Setup
  clipfactory.spec      PyInstaller
  installer.iss         Inno Setup (установка в профиль пользователя, тихое обновление)
  make_manifest.py      подписанный latest.json
  tests/e2e_installed.py  проверка установщика на Windows (CI)
tests/                  автотесты (pytest)
```
