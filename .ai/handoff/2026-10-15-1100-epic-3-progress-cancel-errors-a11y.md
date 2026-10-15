# Epic 3 — прогресс, отмена, ошибки, доступность (Stories 3.1–3.3)

## Что сделано

- **Story 3.1 — прогресс и реальная отмена.**
  - Типизированный жизненный цикл:
    - клиентские этапы: `reading_context`, `mapping_workbook`;
    - серверные этапы: `selecting_artifacts`, `preparing_answer`, `running_computation`, `retrying_computation`, `finalizing`;
    - итог: `completed`, `failed`, `cancelled`;
    - служебные: `planned` (фаза 1 книги завершена) и `cancelling`.
  - Текущий этап показывается текстом в постоянном `role="status" aria-live="polite"` с префиксом «Этап: …». Время «Анализ идёт N с» выводится отдельно и не озвучивается на каждом опросе. Спиннера нет.
  - Пока анализ идёт, повторная отправка невозможна: поле и кнопка заблокированы, `submitQuestion` защищён `run.current`, повторный Enter/submit ничего не делает. Появляется кнопка «Отменить анализ», фокус переходит на неё; Esc тоже отменяет анализ.
  - Отмена реальная: `POST /api/analyses/{id}/cancel`. API:
    - больше не обращается к Qwen;
    - останавливает активный sandbox job (`docker rm --force` + kill);
    - удаляет pending plan, не записывает ход в сеанс, освобождает входы/выходы задачи;
    - отвечает `cancelled` только после очистки.
    Затем фокус возвращается в поле вопроса, и можно задать новый вопрос.
  - Сеть/API: 3 подряд неудачных опроса статуса → ошибка `STATUS_UNAVAILABLE` с одним действием «Повторить». Прогресс убирается, а API получает best-effort cancel.
- **Story 3.2 — единый контракт ошибок.**
  - `src/taskpane/errors.ts` — каталог `code → действие`: `retry` («Повторить»), `choose_context` («Выбрать другой контекст»), `edit_question` («Изменить вопрос»), `update_excel` и `contact_admin` (только текст).
  - Каждая ошибка показывается одной панелью: сообщение, «Следующий шаг: …» и не больше одной кнопки.
  - Тексты с признаками traceback/путей/секретов заменяются общим сообщением. Сырые исключения Excel и сети не показываются.
  - Серверные `nextAction` переписаны так, чтобы в каждом было одно действие.
  - До Qwen и песочницы не доходят:
    - пустой контекст (в том числе все ячейки пустые — новая проверка `hasAnalyzableData`);
    - недоступный контекст;
    - превышение лимита ячеек или листов;
    - вопрос длиннее 4000 символов;
    - невалидный сеанс или план (синхронная валидация до старта задачи).
  - Недостаточность данных: в `Narrative` добавлены поля `insufficientData` и `missingData` (если `insufficientData`, `missingData` обязателен — проверяют и pydantic, и клиент). Промпт требует так отмечать нехватку данных. UI показывает «Данных недостаточно для обоснованного вывода», список недостающих данных и заголовок «Вывод (не подтверждён)».
  - Неподдерживаемый Excel — `role="alert"` до любой попытки анализа, с одним шагом: открыть книгу в Excel Desktop для Windows с ExcelApi 1.1 или обновить Excel.
  - Язык LLM-ответа по-прежнему определяется языком вопроса; системные сообщения — на русском, как локализация панели.
- **Story 3.3 — доступность и прозрачность.**
  - Порядок Tab: scope-кнопки → результаты (копирование источников, `<summary>` Методика/Таблицы/Код/График, копирование кода, кнопка графика) → «Начать новый диалог» → панель ошибки → поле → «Отправить» → «Отменить».
  - Клавиатура: Enter отправляет, Shift+Enter переносит строку.
  - Esc закрывает PNG-просмотр и возвращает фокус на кнопку графика. Если открыт диалог графика, Esc не отменяет анализ.
  - Источники показываются точным текстом `лист · диапазон` (префикс листа из адреса не дублируется). Кнопка «Копировать источники» копирует строки; если clipboard недоступен, текст выделяется и показывается подсказка Ctrl+C. Общий компонент `copy.tsx` используется и для кода.
  - Перехода к диапазону нет: в ExcelApi 1.1 нет надёжного способа сделать это без изменения выделения (`select()` под read-only запретом). Панель прямо об этом пишет.
  - Покрытие книги: `Coverage.sheetsWithData` и `complete`. Для неполного анализа: «Проанализирована только часть книги… Это не анализ всей книги» и перечень листов и диапазонов.
  - Alt и подпись PNG: метрика, период, ключевой вывод (из Epic 2, проверено тестами).
  - Объявления: «Этап: …», «Готово: …», «Ошибка: … Следующий шаг: …», «Отменено: …». Смысл передаётся текстом, а цветная полоса только дублирует его.

## Архитектура прогресса и отмены

**Выбор: polling типизированного status-endpoint, а не SSE.**

- Короткие запросы надёжно проходят через Vite proxy, корпоративный reverse proxy и Office WebView2. Долгий SSE/HTTP-запрос (расчёт до нескольких минут) рвётся на таймаутах прокси.
- Потерю связи легко обнаружить (неудачный опрос), и устаревший прогресс никогда не остаётся «активным».
- Нет долгоживущих соединений и потоков на событийном цикле.
- Цена — задержка до 0,5 с и лишние запросы; для task pane это приемлемо.

**Устройство.**

- `api/app/application/lifecycle.py`:
  - `AnalysisJobs` — RAM-реестр задач по `analysisId`;
  - `AnalysisControl` — `report`, `checkpoint`, `call`, `commit`.
- Каждый анализ выполняется в фоновом потоке после синхронной валидации (`prepare_*` → `execute_*`).
- `control.call(provider.*)` выполняет вызов Qwen во вспомогательном потоке. При отмене воркер сразу перестаёт ждать и больше не обращается к Qwen; поздний ответ отбрасывается.
- `SandboxJob.cancel` (`threading.Event`) — раннер опрашивает его каждые 0,1 с: и в ожидании слота, и во время работы контейнера. При отмене: `docker rm --force --volumes <name>`, kill CLI, вывод не парсится, `SandboxCancelled`.
- **Гонки.**
  - Единственная точка публикации — `commit()`. Запись хода в сеанс и переход в `finalizing` выполняются под lock реестра вместе с проверкой отмены.
  - Если отмена пришла раньше — ничего не записано, результат `cancelled`. Если позже — отмена отклоняется, анализ завершается `completed`, а клиент показывает ответ с пометкой «Отмена пришла после завершения анализа».
  - Клиент сам никогда не решает исход гонки: после нажатия «Отменить» исход определяет ответ API на cancel.
  - Отмена до старта создаёт tombstone: поздний start с тем же ID возвращает `cancelled` и не запускается.
- Тайм-аут задачи (`FINLENS_ANALYSIS_TIMEOUT_SECONDS`, 600 с) идёт по тому же пути очистки и даёт `failed`/`ANALYSIS_TIMEOUT`.
- Лимит активных задач (`FINLENS_MAX_ACTIVE_ANALYSES`, 8) → `ANALYSIS_CAPACITY` (503).
- Закрытие или удаление сеанса (смена scope, новый диалог, `pagehide`) отменяет его незавершённые задачи.
- Клиент: `src/taskpane/analysis-run.ts` (`AnalysisRun`). `AbortController` прерывает fetch и ожидания; после каждого шага Excel проверяется флаг отмены; чтение Excel не прерывается, но его результат отбрасывается.

## API-контракты

| Метод | Путь | Ответ |
| --- | --- | --- |
| POST | `/api/analysis` | `202 AnalysisStatus` (stage `preparing_answer`); отказ валидации — `AnalysisStatus{stage:"failed"}` с 404/409/200; 422 — `FailedAnalysisResult` |
| POST | `/api/sessions/{sid}/workbook/plan` | `202` (`selecting_artifacts`) → по опросу `planned` + `plan` |
| POST | `/api/sessions/{sid}/workbook/analysis` | `202` (`preparing_answer`), возобновляет ту же задачу `planned` |
| GET | `/api/analyses/{analysisId}` | `AnalysisStatus`; неизвестный ID — 404 `ANALYSIS_NOT_FOUND` |
| POST | `/api/analyses/{analysisId}/cancel` | `AnalysisStatus` после очистки: `cancelled` / уже итоговый / `cancelling` (если очистка дольше 20 с) |

`AnalysisStatus = {analysisId, stage, computationAttempt (1..3|null), maxComputationAttempts: 3, elapsedMs, plan (только в planned), result (только в итоговом этапе, status == stage)}`.

`CancelledAnalysisResult = {analysisId, status:"cancelled", message, nextAction, elapsedMs}` — без narrative, sources и computation.

Повторный start с тем же ID: итоговая задача → её статус (200); выполняющаяся → 409 `ANALYSIS_CONFLICT`.

## Гарантии очистки

- **При отмене или тайм-ауте:**
  - контейнер удаляется (`rm --force` + `--rm`; тест реального Docker: 0 оставшихся контейнеров);
  - pending plan удаляется (`discard_pending_plan`, хук отмены выполняется и при отмене во время планирования);
  - ход сеанса не записывается;
  - у задачи обнулены `control`, `plan`, `timer`, `on_cancel`; замыкание с запросом и артефактами освобождается после выхода воркера;
  - результат — только `CancelledAnalysisResult`.
- Итоговые записи реестра хранятся 300 с (повторный опрос после потерянного ответа), затем удаляются. Реестр ограничен 256 записями. Всё только в RAM.
- Брошенный вызов Qwen завершается в фоне (≤ 55 с, таймаут HTTP), его ответ отбрасывается.

## Изменённые файлы

- **Backend.**
  - Новые: `api/app/application/lifecycle.py`, `api/tests/test_lifecycle.py`.
  - Изменённые: `api/app/domain/contracts.py`, `api/app/application/{agent,analyze,workbook,sessions}.py`, `api/app/adapters/{sandbox,qwen}.py`, `api/app/main.py`.
  - Тесты: `api/tests/{support,test_agent,test_sandbox_runner,test_sandbox_docker,test_workbook_sessions,run_sandbox_suite}.py`.
- **Task pane.**
  - Новые: `src/taskpane/{lifecycle.ts,errors.ts,analysis-run.ts,copy.tsx,format.ts}`.
  - Новые тесты: `src/taskpane/{analysis-run.test.ts,static-guards.test.ts}`.
  - Изменённые: `src/taskpane/{analysis-api.ts,App.tsx,AnalysisResultView.tsx,contracts.ts,excel-context.ts,styles.css}`, `src/test/fake-excel.ts` (опция `blank`).
  - Переписанные тесты: `App.test.tsx`, `analysis-api.test.ts`, `AnalysisResultView.test.tsx`.
- `README.md` (раздел Epic 3), `sprint-status.yaml`, этот handoff и `2026-10-14-1800-epic-3-in-progress.md`.

## Тесты (что покрыто)

- **Backend `test_lifecycle.py`:**
  - асинхронный старт (202) и все серверные этапы, включая `retrying_computation` с номером попытки и `selecting_artifacts` → `planned`;
  - отмена во время Qwen: < 2 с, больше нет вызовов Qwen, tool call не дошёл до песочницы, история пустая;
  - отмена во время песочницы и retry: job получил сигнал;
  - гонки: отмена прямо перед готовым ответом, отмена после commit, отмена после завершения, отмена до старта;
  - отмена `planned` удаляет план; поздняя фаза 2 → `cancelled`;
  - отсутствие состояния задачи после отмены; закрытие сеанса отменяет анализ;
  - логи отмены без данных; тайм-аут; capacity/conflict; 404 status;
  - внутренняя ошибка без утечки текста; невалидный/слишком большой контекст не доходит до Qwen;
  - `insufficientData`.
- **Адаптер и Docker:**
  - отмена с поддельным docker CLI: `rm --force`, без контейнера при ранней отмене, отмена в ожидании слота;
  - argv `docker run` идентичен с cancel и без него — все флаги ограничений на месте;
  - отмена реального работающего контейнера;
  - частичное покрытие книги.
- **Клиент:**
  - `analysis-run.test.ts` — порядок всех этапов, отмена на каждом серверном этапе, `cancelling` → `cancelled`, гонка «completed пришёл после клика отмены» → `cancelled`, commit раньше отмены → ответ с пометкой, отмена до первого запроса, потеря статуса → `STATUS_UNAVAILABLE` + cancel, единичный сбой опроса терпится, неподтверждённая отмена, пустой контекст без запросов, сброс сеанса;
  - `App.test.tsx` — Tab-порядок, Enter/Shift+Enter/slash-команды, фокус на «Отменить анализ», блокировка повторной отправки, Esc-отмена, объявления aria-live для этапа/успеха/ошибки/отмены, одна кнопка «Повторить» и повторная отправка, недоступный API, пустой и слишком большой контекст, длинный вопрос, частичное покрытие книги, Esc графика с возвратом фокуса, недостаточность данных, типизированные ошибки Qwen/песочницы с ровно одним действием и без traceback;
  - `AnalysisResultView.test.tsx` — копирование источников и fallback, покрытие, недостаточность данных;
  - `static-guards.test.ts` — нет Office.js write API, browser storage и секретов в `src`.

## Выполненные проверки (macOS, Docker Desktop 29.1.3)

- `npm test` — 10 файлов, 77 тестов OK.
- `npm run typecheck`, `npm run lint` (0 ошибок и предупреждений), `npm run build` — OK.
- `npm run test:api` — 98 тестов OK, включая реальные Docker-тесты.
- `npm run test:sandbox` — 76 тестов OK, без пропусков (добавлен `test_lifecycle`). После тестов контейнеров `finlens.sandbox-job` — 0.
  - Первый запуск упал: Docker на мгновение не ответил на `image inspect`. Повтор прошёл. Перед этим образ `finlens-sandbox:1` пришлось пересобрать (`npm run sandbox:build`), потому что он отсутствовал в Docker.
- `xmllint --noout manifest.xml` — OK.
- **Статические сканы:**
  - Office.js write API в `src` — нет;
  - `localStorage/sessionStorage/indexedDB/document.cookie` в `src`, `api/app`, `config`, `index.html` — нет;
  - Qwen-секреты в `src`, `config`, `manifest.xml`, `index.html` — нет;
  - небезопасные Docker-флаги — нет (единственное совпадение `--volumes` — флаг очистки в `docker rm`);
  - `USER 10001:10001` на месте.
- `git diff --check` — OK. Все файлы untracked, поэтому дополнительно выполнен grep trailing whitespace. Единственное совпадение — исходная строка в planning-артефакте `DESIGN.md` (не менялся).
- **Живой smoke (uvicorn + fake Qwen + реальный контейнер):**
  - `[fake:long]` → `running_computation 1`, контейнер запущен, cancel → `cancelled` за 0,12 с, контейнеров 0;
  - `[fake:retry]` → `completed`, attempts=2;
  - `[fake:slow]` + cancel → `cancelled`.

## Остаточные риски

- Брошенный HTTP-вызов Qwen не прерывается на уровне сокета: сервер Qwen доработает запрос, а поток API живёт до таймаута (55 с). Дальнейших вызовов нет.
- Реестр задач и сеансы живут в одном процессе API. Несколько worker/реплик требуют sticky routing по `analysisId`/`sessionId`.
- Отмена после commit отклоняется честно, и ответ показывается. Если продукт хочет «отмена всегда скрывает ответ», это решение UX.
- Polling 0,5 с: при 8 активных анализах это ~16 запросов/с — приемлемо, но стоит учесть в rate limit прокси.
- `cancel` ждёт очистки до 20 с (синхронный endpoint в threadpool). При очень медленном Docker клиент получит `cancelling` и дождётся `cancelled` опросом.
- Production Qwen не вызывался: поведение `insufficientData` и соблюдение промпта не проверены. Excel Desktop Windows и экранный диктор не проверялись (macOS).
- Переход к диапазону намеренно не реализован (см. Story 3.3).

## Ручная проверка Windows / Docker

1. Docker Desktop (WSL2) запущен; `npm run sandbox:build`; `npm run test:sandbox` — 76 OK; `docker ps -a --filter label=finlens.sandbox-job` пуст.
2. `set FINLENS_QWEN_PROVIDER=fake` → `npm run dev:api`; во втором терминале `npm run dev`; sideload `manifest.xml` (см. `2026-10-14-1650-...md`). Сохранить SHA-256 копии книги.
3. Выделить таблицу, задать вопрос: этапы «Читаю выбранные данные…» → «Готовлю вывод…» → «Готово: …». Enter отправляет, Shift+Enter переносит строку.
4. «Посчитай [fake:retry]»: «Выполняю расчёт…», затем «Исправляю расчёт (попытка 1 из 2)…» (этап может пролететь быстро), ответ «с 2-й попытки».
5. «[fake:slow]»: фокус на «Отменить анализ», кнопка «Отправить» заблокирована. Нажать «Отменить анализ» (или Esc): «Отменяю анализ…» → панель «Анализ отменён», фокус в поле, можно спросить снова.
6. «Посчитай [fake:long]»: в момент «Выполняю расчёт…» в PowerShell `docker ps --filter label=finlens.sandbox-job` показывает контейнер. Отменить — контейнер исчезает за ~1 с.
7. Во время анализа остановить API (Ctrl+C): через ~1,5 с прогресс исчезает, панель «Связь с FinLens API потеряна…» с одной кнопкой «Повторить». Запустить API, нажать «Повторить» — ответ.
8. Выделить пустые ячейки → «нет анализируемых данных» и кнопка «Выбрать другой контекст» (фокус на кнопках scope). Выделить > 10 000 ячеек → отправка заблокирована, объяснение лимита.
9. `/workbook`: в ответе «Проанализирована только часть книги… Это не анализ всей книги» и источники `лист · диапазон`. «Копировать источники» → вставить в Блокнот.
10. «[fake:insufficient]» → блок «Данных недостаточно…», «Вывод (не подтверждён)».
11. Остановить Docker Desktop, «[fake:compute]» → песочница недоступна, следующий шаг «Сообщите администратору», кнопки нет.
12. Пройти шаги 3–10 только клавиатурой (Tab/Shift+Tab/Enter/Esc) и с Экранным диктором: озвучиваются «Этап…», «Готово…», «Ошибка… Следующий шаг…», «Отменено…»; график: Enter открывает, Esc закрывает и возвращает фокус.
13. Логи API: только IDs, коды, длительности, размеры. Закрыть книгу без сохранения, сравнить SHA-256 — книга не изменена.

## Статус

- `sprint-status.yaml`: Stories 3.1–3.3 → `review`; `epic-3: in-progress`. Epic 1 и Epic 2 остаются `in-progress`.

## Точный следующий шаг

Независимый code review Epic 3 (особенно `lifecycle.py`: lock/commit/hooks, `adapters/sandbox.py`: цикл отмены, `analysis-run.ts`: гонки), затем ручная проверка на Windows по шагам выше и с production Qwen.

## Текущий git status

```text
?? .ai/
?? .gitignore
?? README.md
?? _bmad-output/
?? api/
?? config/
?? docs/
?? eslint.config.js
?? index.html
?? manifest.xml
?? package-lock.json
?? package.json
?? sandbox-image/
?? src/
?? tsconfig.json
?? vite.config.ts
```
