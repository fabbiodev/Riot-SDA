# Changelog

All notable changes to this project are documented here. The section whose
heading matches a release tag (e.g. `## v2.1.1`) is used as that release's notes,
with GitHub's auto-generated commit list appended below it.

## В разработке — 1.0.1

- QR-сканер открывается отдельным немодальным окном с иконкой на панели задач. Повторное нажатие возвращает к открытому сканеру; закрытие прекращает захват экрана.
- Добавлены метки редкости скинов LoL и категорий издания VALORANT в сетке и подробностях.
- Поиск учитывает тип скина. Публичные справочники дополняют уже сохранённые коллекции и кэшируются на сутки.
- Размеры интерфейса и текста зафиксированы в физических пикселях независимо от масштаба Windows; ручное изменение окна сохранено.

## v1.0.0

Первый релиз Riot SDA.

### Добавлено
- Профили League of Legends и VALORANT с регионом, уровнем и поиском по Riot ID / логину.
- Сетка чемпионов, агентов и скинов с изображениями, поиском и компактным режимом.
- Эмблемы рангов Solo / Duo, Flex и TFT Ranked в профиле и на карточках аккаунтов.
- Обновление рангов при запуске, после входа и каждый час: OP.GG для Solo / Flex, Riot TFT API для TFT.
- Сохранение QR-сессий через Windows DPAPI, показ времени токена и автоматическое продление, пока его разрешает Riot.

### Исправлено
- Настроен DirectWrite для Inter Variable, включено сглаживание текста в профиле и карточках.
- Отступы минимального окна скорректированы под новые метрики шрифта.

### Установка
- `Riot2FA-Setup.exe` — установщик Windows с ярлыками.
- `Riot2FA.exe` — переносимая сборка; первый запуск включает распаковку зависимостей.
- Файлы `.sha256` содержат контрольные суммы соответствующих сборок.

Личная коллекция загружается из клиента игры. Для TFT Ranked нужен ключ TFT API.
Срок сессии определяет Riot; после её отзыва нужен повторный вход.

## v2.1.5 (история исходного проекта)

### Fixed
- Fresh push registration now sends the Android app identity required by the
  restricted Firebase key, so new devices can receive login approvals.

## v2.1.4

### Added
- Installable Windows build with Start Menu and optional desktop shortcuts.
- Faster-starting folder distribution alongside the portable executable.

### Improved
- Renamed "Add via Login" to "Add an account" and removed manual account entry.
- Load QR scanner libraries only when scanning starts.
- Prefer the installer for in-app updates.

## v2.1.3

### Fixed
- Fixed a crash on startup in v2.1.2 ("No module named 'logging.handlers'") that
  prevented the app from opening. If you're on v2.1.2, download this build
  manually from the releases page — the broken build cannot auto-update itself.

## v2.1.2

### Fixed
- Push notifications now arrive reliably. A bug in the FCM library made it crash
  while decrypting an incoming push ("Incorrect padding" / "Invalid EC key") and
  shut the whole listener down, so no login approvals were delivered. Decryption
  is now padded correctly, and a single undecryptable message is skipped instead
  of killing the listener.

### Added
- The "update available" prompt now shows the new version's release notes.

## v2.1.1

### Fixed
- Login-approval prompts no longer pile up on every launch. The FCM listener now
  persists which pushes it has seen and acknowledges them, so the server stops
  replaying its backlog of old login attempts.
- Auto-update no longer crashes with "Failed to load python312.dll" on restart.
  The updater clears the PyInstaller onefile hand-off variables before relaunch,
  downloads on a background thread with a progress bar, verifies the download,
  and runs the swap silently (no console windows).

### Added
- Detailed error dialog: a clear message plus an expandable, copy-pasteable pane
  showing the full server response (status, headers, body) for easy bug reports,
  with a Discord contact for help.
- Share & import accounts with a single code. Sharing carries the 2FA seed and
  sign-in session; on import the push device is re-registered so login approvals
  arrive on the importing machine.

## v2.1.0

### Changed
- Add-via-Login now uses a bundled stealth Chromium (Patchright) instead of the
  Qt WebEngine browser, which the login captcha was blocking.
