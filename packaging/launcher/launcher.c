/* ClipFactory.exe — launcher for Windows.
 *
 * First run: installs everything with app\setup.bat (visible console window).
 * Next runs: starts app\start.bat, which opens the program in the browser
 * (or just opens the browser if ClipFactory is already running).
 */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <wchar.h>

static const wchar_t *TITLE = L"ClipFactory";

static int file_exists(const wchar_t *path) {
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static int run_console(const wchar_t *script, const wchar_t *cwd, int wait) {
    wchar_t cmd[MAX_PATH * 2];
    swprintf(cmd, MAX_PATH * 2, L"cmd.exe /c \"%ls\"", script);
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NEW_CONSOLE, NULL, cwd, &si, &pi)) {
        return -1;
    }
    DWORD code = 0;
    if (wait) {
        WaitForSingleObject(pi.hProcess, INFINITE);
        GetExitCodeProcess(pi.hProcess, &code);
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (int)code;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR args, int show) {
    (void)inst; (void)prev; (void)args; (void)show;
    wchar_t base[MAX_PATH], app[MAX_PATH], probe[MAX_PATH], venv[MAX_PATH];

    if (!GetModuleFileNameW(NULL, base, MAX_PATH)) return 1;
    wchar_t *slash = wcsrchr(base, L'\\');
    if (slash) *slash = 0;

    swprintf(app, MAX_PATH, L"%ls\\app", base);
    swprintf(probe, MAX_PATH, L"%ls\\backend\\main.py", app);
    if (!file_exists(probe)) {
        MessageBoxW(NULL,
            L"Не найдена папка «app» с программой.\n\n"
            L"ClipFactory.exe должен лежать в той же папке, что и папка «app». "
            L"Распакуйте архив ClipFactory.zip целиком (правой кнопкой → «Извлечь все…») "
            L"и запускайте ClipFactory.exe из распакованной папки.",
            TITLE, MB_OK | MB_ICONERROR);
        return 1;
    }

    swprintf(venv, MAX_PATH, L"%ls\\.venv\\Scripts\\python.exe", app);
    if (!file_exists(venv)) {
        int answer = MessageBoxW(NULL,
            L"Похоже, ClipFactory запускается впервые.\n\n"
            L"Сейчас откроется чёрное окно установки. Оно скачает нужные компоненты "
            L"(5–15 минут, нужен интернет) и задаст 1 вопрос про FFmpeg — нажмите Y.\n\n"
            L"Когда в окне появится «Установка завершена», нажмите любую клавишу — "
            L"программа откроется в браузере.\n\nНачать установку?",
            TITLE, MB_OKCANCEL | MB_ICONINFORMATION);
        if (answer != IDOK) return 0;
        if (run_console(L"setup.bat", app, 1) < 0) {
            MessageBoxW(NULL, L"Не удалось запустить установку (setup.bat).", TITLE, MB_OK | MB_ICONERROR);
            return 1;
        }
        if (!file_exists(venv)) {
            MessageBoxW(NULL,
                L"Установка не завершилась. Прочитайте сообщение в окне установки и файл "
                L"«Инструкции\\7. Если что-то не работает.txt», затем запустите ClipFactory.exe ещё раз.",
                TITLE, MB_OK | MB_ICONWARNING);
            return 1;
        }
    }

    if (run_console(L"start.bat", app, 0) < 0) {
        MessageBoxW(NULL, L"Не удалось запустить ClipFactory (start.bat).", TITLE, MB_OK | MB_ICONERROR);
        return 1;
    }
    return 0;
}
