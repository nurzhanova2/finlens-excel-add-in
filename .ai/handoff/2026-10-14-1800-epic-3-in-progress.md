# Epic 3 (Stories 3.1–3.3) — in progress

## Что сделано

- Прочитаны все handoff (включая Epic 2, Epic 1 blockers, Story 1.4), Stories 3.1–3.3 в `epics.md`, PRD, Architecture Spine, DESIGN, EXPERIENCE, sprint status, код `src/taskpane/`, `api/app/`, `sandbox-image/`, тесты, README, `package.json`.
- Docker 29.1.3 доступен, образ `finlens-sandbox:1` есть.

## Решения (план)

- **Прогресс: polling типизированного status-endpoint, не SSE.** Start-эндпоинты (`POST /api/analysis`, `.../workbook/plan`, `.../workbook/analysis`) после синхронной валидации возвращают `202` + `AnalysisStatus`; задача выполняется в фоне в RAM-реестре `AnalysisJobs`. Клиент опрашивает `GET /api/analyses/{analysisId}`; отмена — `POST /api/analyses/{analysisId}/cancel`.
- Кооперативная отмена: `AnalysisControl` (checkpoint/report/call/commit). Вызов Qwen выполняется во вспомогательном потоке и бросается при отмене; песочница опрашивает cancel-event и делает `docker rm --force` + kill. Флаги контейнера не меняются.
- Гонка cancel/completed решается одной точкой commit под lock реестра: запись хода в сессию и переход в `finalizing` атомарны с проверкой отмены.
- Stage enum: `reading_context`, `mapping_workbook`, `selecting_artifacts`, `preparing_answer`, `running_computation`, `retrying_computation`, `finalizing`, `completed`, `failed`, `cancelled` (+ служебные `planned`, `cancelling`).
- Story 3.2: единый каталог ошибок клиента (`errors.ts`) с одним действием; `insufficientData`/`missingData` в narrative.
- Story 3.3: копирование источников, явное частичное покрытие книги, Enter/Esc, фокус на «Отменить анализ».

## Точный следующий шаг

Backend: `application/lifecycle.py` (реестр задач), отмена в `agent.py` и `adapters/sandbox.py`, рефакторинг `analyze.py`/`workbook.py` на prepare/execute, новые эндпоинты в `main.py`.

## Обновление — backend готов

- Новое: `api/app/application/lifecycle.py` (`AnalysisJobs`, `AnalysisControl`, `AnalysisCancelled`), `api/tests/test_lifecycle.py` (21 тест).
- Изменено: `contracts.py` (`AnalysisStatus`, `CancelledAnalysisResult`, `ServerStage`, `Coverage.sheetsWithData/complete`, `Narrative.insufficientData/missingData`), `agent.py` (report/call/checkpoint), `adapters/sandbox.py` (`SandboxJob.cancel`, `SandboxCancelled`, опрос отмены 0,1 с, `docker rm --force` + kill; флаги не менялись), `analyze.py`/`workbook.py` (prepare/execute + commit), `main.py` (202 + статус, `GET /api/analyses/{id}`, `POST /api/analyses/{id}/cancel`, закрытие сеанса отменяет задачи), `sessions.py` (`discard_pending_plan`), `qwen.py` (промпт insufficientData; fake `[fake:insufficient|slow|long]`), тексты `nextAction` — одно действие.
- Тесты обновлены под асинхронный контракт (`tests/support.follow`). Новые: отмена в адаптере (fake docker) и в реальном Docker, флаги не меняются при cancel, частичное покрытие книги.
- `npm run test:api` — 98 тестов OK (с реальным Docker; образ пересобран `npm run sandbox:build`, т.к. отсутствовал).

## Точный следующий шаг

Клиент: `lifecycle.ts`, `errors.ts`, `analysis-api.ts` (статус/отмена/polling), `analysis-run.ts` (оркестратор с отменой), `App.tsx`, `AnalysisResultView.tsx` (копирование источников, покрытие, недостаточность данных), тесты.

## Итог

Epic 3 (Stories 3.1–3.3) реализован, Stories переведены в `review`; актуальное состояние — `2026-10-15-1100-epic-3-progress-cancel-errors-a11y.md`.
