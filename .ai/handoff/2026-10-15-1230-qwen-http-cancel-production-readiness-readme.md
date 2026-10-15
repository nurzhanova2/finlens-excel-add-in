# Отмена Qwen на уровне HTTP, production-границы и двуязычный README

Продолжение `2026-10-15-1100-epic-3-progress-cancel-errors-a11y.md`. Промежуточный файл этого шага: `2026-10-15-1200-qwen-cancel-readiness-in-progress.md`.

## 1. Отмена Qwen — что изменилось

**Было:** при отмене воркер переставал ждать вызов Qwen, но HTTP-запрос (`urlopen`) продолжал жить в фоновом потоке до 55 с, сервер Qwen дорабатывал запрос.

**Стало:** отмена закрывает сокет активного запроса, поток вызова завершается, и только после этого API отвечает `cancelled`.

- **Новый `api/app/domain/cancellation.py`:**
  - `CancelSignal` — подкласс `threading.Event` с `on_cancel(callback) -> unregister`. Callback выполняется ровно один раз при `set()` (или сразу, если сигнал уже выставлен); исключения callback логируются только типом.
  - `CallCancelled` — исключение адаптера, вызов которого прерван.
  - Песочница по-прежнему опрашивает `is_set()`: тип совместим.
- **`api/app/adapters/qwen.py`:**
  - `QwenProvider.next_turn(..., cancel=None)` и `select_ranges(..., cancel=None)`.
  - `HttpQwenProvider` переведён с `urllib.urlopen` на `http.client` (stdlib, новых зависимостей нет):
    - явный `connect()`, затем проверка сигнала: отмена во время connect не отправит запрос;
    - зарегистрированный callback делает `socket.socket.shutdown(sock, SHUT_RDWR)`. Блокирующий `recv` в потоке вызова сразу возвращается (для TLS тоже: базовый `shutdown`, SSL-объект читателя не трогается);
    - любое исключение после отмены превращается в `CallCancelled`; без отмены — `QWEN_UNAVAILABLE` (сеть, таймаут, non-2xx, битый JSON) или `QWEN_INVALID_RESPONSE`, как раньше;
    - `finally`: снять callback, `connection.close()`;
    - таймаут сокета — поле `timeout_seconds` (по умолчанию `QWEN_HTTP_TIMEOUT_SECONDS = 55`);
    - URL со схемой не `http/https`, без хоста или с `user:pass@` → `QWEN_NOT_CONFIGURED` до любого запроса;
    - **изменение поведения:** редиректы не выполняются, переменные `HTTP(S)_PROXY` не используются (`urlopen` их учитывал). Задокументировано в README и production checklist.
  - Тексты ошибок не содержат URL, ключ или тело ответа; адаптер ничего не логирует.
  - Fake `[fake:slow]` ждёт `cancel.wait(8 с)` вместо `sleep` и выбрасывает `CallCancelled`.
- **`api/app/application/lifecycle.py`:**
  - `AnalysisControl.cancel_event` — теперь `CancelSignal`; `call(fn, *args)` вызывает `fn(*args, cancel=signal)`. `CallCancelled` → `AnalysisCancelled`.
  - `_JobControl.call`: вызов, как и раньше, идёт во вспомогательном потоке `finlens-model-call` (защита от провайдера, игнорирующего сигнал). При отмене воркер ждёт до `MODEL_CALL_ABORT_GRACE_SECONDS = 2` с (параметр `AnalysisJobs(model_abort_grace_seconds=…)`), пока вызов реально завершится, затем `AnalysisCancelled`.
  - Логи: `model_call_aborted analysisId=… unwindMs=…` или `model_call_abandoned analysisId=… waitedMs=…` (если провайдер не уложился в 2 с). Только метаданные.
  - Поскольку `cancel()` ждёт `worker_done`, ответ `cancelled` приходит после закрытия HTTP-запроса, удаления контейнера и выполнения хуков очистки.
- **`api/app/main.py`:** комментарий о том, что сеансы, задачи и слоты песочницы живут в одном процессе.

Sandbox security, флаги `docker run` и read-only гарантия Excel не менялись (клиентский код не трогался).

## 2. Модель гонок cancel / completed

Единственная точка публикации — `AnalysisJobs._commit` (запись хода в сеанс + переход в `finalizing`) под lock реестра, атомарно с проверкой сигнала. Возможные порядки:

| Порядок событий | Итог | История | Ответ на cancel |
| --- | --- | --- | --- |
| cancel во время HTTP-запроса | сокет закрыт → `CallCancelled` → `cancelled` | пусто | `cancelled` (после закрытия запроса) |
| ответ Qwen прочитан, cancel до `commit` | `checkpoint` после вызова → `cancelled`, ответ отброшен | пусто | `cancelled` |
| cancel пришёл во время чтения ответа | shutdown прерывает чтение, либо проверка после `read()` → `CallCancelled` | пусто | `cancelled` |
| `commit` раньше cancel | cancel отклонён (`job.committed`) | 1 ход | `finalizing`/`completed`; клиент показывает ответ с пометкой |
| cancel до старта | tombstone, поздний start → `cancelled` | пусто | `cancelled` |
| таймаут задачи | тот же путь, `failed/ANALYSIS_TIMEOUT` | пусто | — |

- Поздний completed-result после `cancelled` невозможен:
  - `_finish` при выставленном сигнале и `committed=False` всегда даёт `cancelled`;
  - итоговое состояние больше не меняется;
  - поток вызова завершён (или брошен с логом `model_call_abandoned`), его результат никуда не попадает.
- Callback закрытия сокета выполняется в потоке, который вызывает `set()`. Это происходит внутри `AnalysisJobs.cancel` под lock реестра, но callback — один неблокирующий `shutdown()`.

## 3. Тесты

**Новый `api/tests/test_qwen_cancellation.py` (12 тестов).** Использует реальный локальный OpenAI-совместимый HTTP-сервер `tests.support.FakeQwenServer`: он удерживает запрос (`HOLD`) и фиксирует разрыв соединения клиентом (`disconnected`).

- **Адаптер:**
  - `test_cancel_aborts_the_in_flight_request` — `next_turn` возвращается < 1 с с `CallCancelled`, сервер видит disconnect;
  - `test_cancel_aborts_workbook_selection_request` — то же для `select_ranges`;
  - `test_already_cancelled_call_sends_nothing` — запрос не отправляется;
  - `test_timeout_stays_a_typed_error_without_secrets` — `QWEN_UNAVAILABLE`, без ключа и адреса в тексте;
  - `test_http_error_status_is_typed` — 500 → `QWEN_UNAVAILABLE`;
  - `test_unsupported_endpoint_urls_are_rejected_before_any_request`;
  - `test_cancel_signal_callbacks_run_once_and_can_unregister`.
- **Через API (TestClient + `HttpQwenProvider`):**
  - `test_cancel_during_qwen_closes_the_request_and_cleans_up`:
    - `cancelled` < 2 с; потоков `finlens-model-call` не осталось; сервер видит disconnect;
    - `job.control/timer/on_cancel` очищены, `active_count == 0`, история и артефакты сеанса пусты;
    - поздний ответ сервера ничего не меняет; повторных запросов к Qwen нет.
  - `test_new_question_after_cancel_works` — второй вопрос в том же сеансе `completed`, в истории ровно 1 ход.
  - `test_cancel_during_workbook_selection_closes_the_request_and_drops_the_plan`.
  - `test_cancel_logs_have_metadata_only` — есть `model_call_aborted`; нет ключа, `Bearer`, ячеек, вопроса и адреса сервера.
- **Гонка:** `CompletedCancelRaceTests.test_race_has_exactly_one_consistent_outcome` — 12 раундов, ответ сервера и cancel стартуют через `Barrier`.
  - Чётные раунды дают фору отмене, нечётные — ответу (до ~0,1 с). Тест требует, чтобы встретились **оба** исхода.
  - Инварианты:
    - `cancelled` ⇔ пустая история, без narrative, ответ на cancel `cancelled`;
    - `completed` ⇔ 1 ход, ответ на cancel `finalizing`/`completed`;
    - статус через 0,2 с не меняется; к Qwen ровно 1 запрос.
  - Прогнано 3×: стабильно 6/6.

**Изменённые тесты:**

- `test_agent.HttpToolCallingTests` и `test_workbook_sessions.test_http_provider_sends_structured_prompt…` переведены с патча `urlopen` на `FakeQwenServer` (проверяют реальный заголовок `Authorization`);
- тестовые провайдеры принимают `cancel`;
- `GatedProvider` в `test_lifecycle` ждёт gate или сигнал (`wait_gate`).

`api/tests/run_sandbox_suite.py` теперь включает `tests.test_qwen_cancellation`.

## 4. Production-границы (документация, без инфраструктуры)

- `docs/deployment/production-checklist.md` (EN/RU):
  - **single instance / sticky routing** — всё состояние в памяти одного процесса; рестарт очищает диалоги; масштабирование только через sticky routing по `sessionId`/`analysisId` или отдельное решение об общем *эфемерном* хранилище (не реализовано);
  - единый HTTPS origin через reverse proxy;
  - rootless Docker / gVisor (`runsc`) / отдельный sandbox-хост через `DOCKER_HOST`; Docker daemon недоступен API без изоляции;
  - секреты Qwen только в серверном хранилище секретов;
  - лимиты, алерты по кодам, контроль оставшихся контейнеров, хранение audit-метаданных, отсутствие health-endpoint.
- `docs/acceptance/manual-acceptance.md` (EN/RU): разделы A (Excel Desktop Windows), B (production Qwen), C (NVDA/Narrator + клавиатура), D (Docker sandbox), E (SHA-256 `.xlsx` до/после); статус «не выполнялось», журнал прогонов.
- Redis, БД, облачные ресурсы, auth и постоянная история **не добавлялись**.

## 5. README

[`README.md`](../../README.md) переписан: равноправные разделы **English** и **Русский** по 9 пунктов:

1. продукт и аудитория;
2. интерфейс со схемой панели и всеми элементами;
3. сценарий, slash-команды, клавиши, советы и примеры вопросов RU/EN;
4. таблица этапов и полная таблица кодов ошибок с действием и кнопкой;
5. границы продукта;
6. security/privacy простым языком;
7. локальный запуск, сертификаты, Docker, fake Qwen, production-переменные с плейсхолдерами;
8. команды проверок и статические сканы, проверенные запуском;
9. ограничения, ссылки на чек-листы и открытые внешние проверки.

Реальных ключей и внутренних адресов нет; везде указано, что внешние проверки не проводились.

## 6. Выполненные проверки (macOS, Docker Desktop 29.1.3)

- `npm test` — 10 файлов, 77 тестов OK.
- `npm run typecheck`, `npm run lint` (0 замечаний), `npm run build` — OK.
- `npm run test:api` — 110 тестов OK (Docker доступен, без пропусков).
- `npm run test:sandbox` — 88 тестов OK, без пропусков; контейнеров `finlens.sandbox-job` после прогона — 0.
- `xmllint --noout manifest.xml` — OK; `python3 -m compileall -q api/app` — OK.
- Статические сканы из README (Office.js write API, browser storage, клиентские секреты, небезопасные Docker-флаги) — совпадений нет.
- `git diff --check` — OK. Все файлы untracked, поэтому отдельно выполнен grep trailing whitespace по изменённым и новым файлам — чисто.
- **Живой smoke** (uvicorn + fake Qwen):
  - `[fake:slow]` → cancel → `cancelled` за 0,01 с;
  - следующий вопрос в том же сеансе → `completed`.

## 7. Внешние проверки, которые нельзя закрыть автоматически

1. Excel Desktop для Windows: sideload и полный сценарий (`manual-acceptance.md`, раздел A).
2. Production Qwen:
   - качество ответов и выбора диапазонов, tool calling, `tool_choice`/`response_format`, `insufficientData`;
   - время: ≤ 1 мин простой ответ, ≤ 5 мин с расчётом;
   - поведение сервера при разрыве соединения отменой;
   - прямой доступ без proxy-переменных.
3. NVDA или Экранный диктор Windows: объявления, порядок Tab, диалог графика.
4. Docker sandbox на целевом хосте: rootless / gVisor / отдельный хост; `npm run test:sandbox` там.
5. Побайтовая неизменность `.xlsx` (SHA-256 до/после) после реальных сеансов в Excel.
6. Решения инфраструктуры: reverse proxy и origin, sticky routing или single instance, хранилище секретов, мониторинг и audit-метаданные.
7. Независимый code review Epic 1–3, включая `domain/cancellation.py`, `adapters/qwen.py` (`_send`) и `lifecycle.py` (`_JobControl.call`).

## Статус

- `sprint-status.yaml`: статусы **не менялись** (Stories 1.2–1.4, 2.1–2.3, 3.1–3.3 — `review`; Story 1.1 — `backlog`; Epic 1–3 — `in-progress`). Обновлено только `last_updated`.
- В `done` ничего не переводится до независимого review, production Qwen, Windows Excel и проверки с экранным диктором.

## Точный следующий шаг

Независимый code review изменений этого шага и Epic 3, затем прогон `docs/acceptance/manual-acceptance.md` на Windows с production Qwen.

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

## Обновление — README в стиле проекта

По просьбе пользователя README переписан по образцу `github.com/nurzhanova2/leap-ippu-decarbonization-kz`: центрированная шапка с бейджами, отдельные файлы `README.md` (English) и `README_ru.md` (Русский), блоки IMPORTANT/WARNING, таблицы, раздел структуры. Только описание продукта, без эпиков, stories, handoff и истории разработки. Из `docs/` убраны упоминания Story/Epic и кодов NFR/AD.
