# Riot SDA

Приложение для Windows с профилями League of Legends и VALORANT,
коллекциями, рангами и входом Riot по QR. В интерфейсе используется название Riot Auth.

![Интерфейс Riot SDA с демонстрационными аккаунтами](images/dashboard.png)

## Возможности

- Несколько аккаунтов, поиск по Riot ID и логину.
- Регион, уровень, чемпионы/агенты и скины аккаунта.
- Сетка карточек с изображениями, поиск по названиям и английским псевдонимам,
  подробности по двойному щелчку или Enter.
- Solo / Duo и Flex через открытый API OP.GG без ключа Riot.
- TFT Ranked через официальный Riot TFT API с отдельным ключом.
- Обновление рангов при запуске, после входа и каждый час, включая работу в трее.
- Сохранение предыдущих рангов при ошибке и учёт лимита OP.GG.
- QR-вход с подтверждением, сохранением сессии через Windows DPAPI и
  автоматическим обновлением токена, пока Riot разрешает продление.
- Подключение аутентификатора 2FA отдельной операцией.

Добавление игрового профиля не требует подключения аутентификатора.
Для подключения Riot Mobile 2FA сохраняется требование Riot о включённой email MFA.
Общий срок SSO-сессии задаёт Riot; при её отзыве нужен обычный повторный вход.

## Запуск из исходников

Проверено на Windows с Python 3.11.

```powershell
git clone https://github.com/fabbiodev/Riot-SDA.git
cd Riot-SDA
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m patchright install chromium
.\.venv\Scripts\python.exe main.py
```

После установки зависимостей можно использовать `start.cmd`.

## Игровые данные

В «Настройки API» выберите сервер для каждого аккаунта и при необходимости
укажите ключи Riot Developer API. Нужен полный Riot ID вида `ник#тег`.
По умолчанию выбран RU, если сервер ранее не задан и не загружен.

Solo / Duo и Flex используют [официальный открытый API OP.GG](https://github.com/opgginc/opgg-mcp).
В его текущем наборе методов нет ранга игрока TFT, поэтому для TFT нужен ключ
с доступом к TFT API. В подсказке ранга показаны источник и время данных:
OP.GG может возвращать свой кэш.

Ключи Riot используются только в текущем запуске. Их также можно задать
через `RIOT_API_KEY`, `RIOT_TFT_API_KEY`, `RIOT_VALORANT_API_KEY`.
Эти ключи отделены от сессии входа Riot.

«Коллекция из клиента» читает данные аккаунта, открытого в соответствующей игре.
Публичный Developer API не выдаёт личный список приобретённых скинов.
Режим «Каталог игры» показывает все объекты и не подтверждает владение ими.
Иконки загружаются из публичных Data Dragon Riot и VALORANT-API и кэшируются.

## Локальные данные

Профили и данные подключённого аутентификатора хранятся в
`%APPDATA%\Riot2FA\accounts.json`. Этот файл может содержать секреты 2FA и
сохранённые старой версией учётные данные; не публикуйте его.

QR-сессии профилей хранятся отдельно в `%APPDATA%\Riot2FA\qr-sessions.dpapi`
с шифрованием Windows DPAPI для текущего пользователя Windows. Они не включаются
в экспорт профиля. Полный выход из программы прекращает фоновые обновления
до следующего запуска.

## Проверки

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Тесты используют искусственные аккаунты и подменённые сетевые ответы.
Предпросмотр интерфейса с публичными изображениями:

```powershell
.\.venv\Scripts\python.exe tests\render_dashboard.py design-preview --live-artwork --grid-fixtures
```

## Сборка Windows

```powershell
.\.venv\Scripts\python.exe -m pip install -r build\requirements-build.txt
$env:RIOT2FA_ONEDIR = '1'
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm build\Riot2FA.spec
```

Результат: `dist/Riot2FA/Riot2FA.exe`. Сохраняйте `_internal` рядом с EXE;
пакет включает браузер для входа Riot, шрифт, эмблемы рангов и зависимости.
Если существует `build/obf/main.py`, spec использует эту обфусцированную версию:
для сборки свежих обычных исходников такой каталог должен отсутствовать.

Исходник иконки: `images/icon.svg`. PNG и ICO пересоздаются командой
`.venv\Scripts\python.exe build\render_app_icon.py`.
Автоматическое обновление из исходного upstream в этой версии отключено.

## Основа и лицензия

Проект основан на [RiotGames-Mobile-2FA-Bypass](https://github.com/Askin242/RiotGames-Mobile-2FA-Bypass).
Авторские уведомления исходного проекта сохранены в [MIT License](LICENSE).
Источники эмблем указаны в [app/assets/ranks/SOURCES.md](app/assets/ranks/SOURCES.md),
лицензия шрифта — в `app/assets/fonts`.
