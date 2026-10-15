# Epic 2 — песочница, исправление ошибок и проверяемые результаты (Stories 2.1–2.3)

## Что сделано

- **Story 2.1 — песочница.**
  - Qwen может запросить только `run_analysis` (OpenAI-compatible `tools`/`tool_calls`).
  - API валидирует tool call до запуска:
    - имя инструмента и один вызов за ход;
    - строгая схема `RunAnalysisArguments` (`code`, `inputs`, `method`, `extra="forbid"`, strict);
    - ссылки `artifact_1..5` только на артефакты текущего анализа;
    - размеры;
    - AST-политика кода: allowlist импортов, запрет `os/sys/subprocess/socket/urllib/importlib/ctypes`, `open/eval/exec/getattr/__import__`, dunder/private-атрибутов, pandas/numpy IO, `savefig`, Excel/Office, URL, путей и секретов в строках;
    - проверка `method` на URL и секреты.
  - Каждый запуск — новый `docker run` образа `finlens-sandbox:1`:
    - non-root 10001;
    - `--network none`, `--read-only`, tmpfs `/tmp` с `noexec,nosuid,nodev` 32 MB;
    - без `-v/--mount/-e`, `--cap-drop ALL`, `no-new-privileges`, `--ipc none`;
    - `--pids-limit 64`, 512 MB RAM без swap, 1 CPU, ulimit nofile/core;
    - `--log-driver none`, `--pull never`.
  - Вход передаётся только через stdin (копии артефактов), результат — JSON через stdout. Host повторно валидирует результат схемой: таблицы, PNG-сигнатура и размеры, лимиты.
  - Очистка: `--rm` и всегда `docker rm --force` в `finally`, в том числе после таймаута или kill. Файлов на host нет.
  - Без Docker или образа — `SANDBOX_UNAVAILABLE`. `FINLENS_SANDBOX_RUNTIME≠docker` отключает расчёты. Fallback на host нет.
- **Story 2.2 — исправление ошибок.**
  - Не более 3 запусков: исходный + 2 исправления. Отклонённый tool call тоже считается попыткой и не доходит до песочницы.
  - Модель получает только `SafeDiagnostic` (код, тип, сообщение ≤ 400 символов, строка) и сводку входов. Пути и секреты вычищаются дважды: в runner и на host.
  - После лимита — `failed`/`COMPUTATION_FAILED`, `computationAttempts=3` и `nextAction`; Qwen больше не вызывается, narrative нет.
  - `basedOnComputation=true` без успешного запуска → `UNVERIFIED_COMPUTATION`.
  - После успеха `tool_choice="none"`.
  - Логи: `analysisId`, номер попытки, outcome/код, длительности, размеры.
- **Story 2.3 — проверяемые результаты.**
  - `CompletedAnalysisResult.computation` содержит только фактически выполненный код, `method`, `attempts`, `inputs`, таблицы, PNG и описание графика (метрика/период/вывод формирует сам выполненный код через `show_chart`).
  - UI показывает ответ, источники и ID анализа, плашку «Расчёт выполнен…» и блоки `<details>`: «Методика», «Таблицы», «Код» (по умолчанию свёрнуты), «График» (раскрыт, PNG по ширине панели).
  - Копирование кода: `navigator.clipboard`, при запрете — выделение кода и подсказка Ctrl+C.
  - Модальный просмотр PNG: фокус на «Закрыть», Tab остаётся внутри, Esc или клик по фону закрывает, фокус возвращается на график.
  - Клиент отклоняет некорректный `computation` (не PNG base64, ячейки-объекты, attempts > 3) и `failed` с артефактами.
  - Ничего не вставляется в Excel.
- Fake Qwen — только при `FINLENS_QWEN_PROVIDER=fake`. Сценарии задаются маркерами `[fake:compute|retry|invalid-tool|fail|unverified]`; код fake не зависит от данных.

## Изменённые файлы

- Новые:
  - `sandbox-image/Dockerfile`, `requirements.txt` (pinned), `finlens_runner.py`, `.dockerignore`;
  - `api/app/domain/computation.py`, `api/app/domain/code_policy.py`, `api/app/adapters/sandbox.py`, `api/app/application/agent.py`, `api/app/application/errors.py`;
  - `api/tests/support.py`, `test_tool_calls.py`, `test_agent.py`, `test_sandbox_runner.py`, `test_sandbox_docker.py`, `run_sandbox_suite.py`;
  - `src/taskpane/AnalysisResultView.tsx`, `AnalysisResultView.test.tsx`.
- Изменённые:
  - `api/app/adapters/qwen.py`, `api/app/application/analyze.py`, `workbook.py`, `api/app/main.py`, `api/app/domain/contracts.py`, `api/tests/test_workbook_sessions.py`;
  - `src/taskpane/analysis-api.ts`, `App.tsx`, `styles.css`, `App.test.tsx`, `analysis-api.test.ts`;
  - `package.json` (`sandbox:build`, `test:sandbox`), `README.md`, `.gitignore` (`__pycache__/`), `sprint-status.yaml`.
- Промежуточный handoff `2026-10-14-1710-epic-2-in-progress.md` и этот файл.

## Модель угроз (кратко)

| Угроза | Контроль |
| --- | --- |
| Prompt injection из ячеек → вредный код | AST-политика на API; в runner ограниченные builtins/импорты; контейнер — основная граница |
| Сеть / exfiltration | `--network none` (тест: нет маршрутов, connect/DNS падают) |
| Чтение host FS / секретов | нет mounts и `-e`; env docker CLI по allowlist (FINLENS_* не передаются); тест: секреты из env API не видны |
| Эскалация привилегий | uid 10001, CapEff=0, NoNewPrivs=1, setuid запрещён, read-only root, tmpfs noexec |
| DoS | CPU rlimit + `--cpus`, RAM без swap, pids, wall-time (alarm в контейнере + жёсткий kill на host), лимит stdout 5,5 MB, ≤ 2 параллельных job |
| Состояние между попытками | новый контейнер на каждую попытку (тест: файл попытки 1 не виден попытке 2) |
| Выдуманные расчёты | `computation` только из успешного job; `UNVERIFIED_COMPUTATION`; после 3 неудач нет narrative |
| Утечка в логи | только IDs, коды, длительности и размеры (тест) |
| Изменение Excel | песочница не имеет Office.js; UI без write API (скан + `fake.writes == []`) |

## Лимиты

| Параметр | Значение |
| --- | --- |
| Код | ≤ 20 000 символов, ≤ 400 строк |
| `method` | ≤ 2 000 символов |
| Входы | ≤ 5 артефактов |
| Попытки | 3 (исходная + 2 исправления) |
| Wall-time | 30 с (+20 с на старт контейнера) |
| CPU | 30 с, 1 CPU |
| Память и процессы | 512 MB без swap, 64 pids |
| tmpfs | 32 MB |
| Таблицы | ≤ 5 таблиц, ≤ 200 строк (с отметкой об обрезке), ≤ 30 столбцов, ≤ 200 символов на ячейку |
| Графики | ≤ 3 PNG по ≤ 1 MB и ≤ 2000 px |
| stdout | ≤ 4 000 символов |
| Диагностика | ≤ 400 символов |

Все значения стартовые и требуют уточнения на пилоте (NFR-08).

## Команды запуска

```bash
npm ci && python -m pip install -r api/requirements.txt
npm run sandbox:build      # нужен запущенный Docker
npm run dev:api            # API (+ FINLENS_QWEN_* или FINLENS_QWEN_PROVIDER=fake)
npm run dev                # task pane https://localhost:5173
npm run test:sandbox       # обязательные security-тесты песочницы
```

## Выполненные проверки (macOS, Docker Desktop 29.1.3)

- `npm test` — 8 файлов, 41 тест OK.
- `npm run typecheck`, `npm run lint`, `npm run build` — OK.
- `npm run test:api` — 71 тест OK, включая реальные Docker-тесты.
- `npm run test:sandbox` — 50 тестов OK, без пропусков.
- `xmllint --noout manifest.xml` — OK.
- Статические сканы:
  - Office.js write API в `src` — нет;
  - `localStorage/sessionStorage/indexedDB/cookie` — нет;
  - секреты Qwen в клиенте, manifest и config — нет;
  - небезопасные флаги песочницы (`--privileged`, `--cap-add`, unconfined, host network, docker.sock, volume/mount/env, `shell=True`) — нет;
  - `USER 10001:10001` в Dockerfile.
- `git diff --check` — OK. Все файлы untracked, поэтому дополнительно выполнен scan trailing whitespace по новым и изменённым файлам — чисто.
- После тестов контейнеров с меткой `finlens.sandbox-job` — 0.
- Live smoke через `uvicorn` (fake Qwen + реальный контейнер):
  - `[fake:retry]` → `completed`, `attempts=2`, таблица и PNG;
  - `[fake:fail]` → `COMPUTATION_FAILED`, `computationAttempts=3`, без `computation`.

## Остаточные риски

- Python-уровень ограничений обходим для изощрённого кода. Граница безопасности — контейнер; для production рекомендован gVisor (`FINLENS_SANDBOX_OCI_RUNTIME=runsc`) или выделенный хост песочницы.
- API получает доступ к Docker daemon, что фактически равно root на хосте API. Нужен rootless Docker или отдельный runner-хост — решение инфраструктуры.
- Код в контейнере теоретически может подделать собственный вывод через объекты runner. Host валидирует только схему, но не «честность» чисел: это самообман модели, не обход изоляции.
- Docker Desktop на macOS: `MEMORY_LIMIT` определяется по exit 137. Длительные job, убитые по CPU, классифицируются как `CPU_LIMIT` по длительности.
- В одном smoke-прогоне провал после 3 попыток занял 26 с (обычно job длится 1–5 с) — разовая задержка Docker Desktop. Цель ≤ 5 мин не нарушена; замерить на целевой инфраструктуре.
- Production Qwen не вызывался: качество tool calling, следование `basedOnComputation` и поддержка `tool_choice` сервером не проверены.
- Прогресс «Исправляю расчёт (попытка N из 2)» в реальном времени невозможен без streaming (Epic 3). Число попыток показывается в результате.
- Excel Desktop Windows не проверялся (macOS).

## Ручной тест Docker / Windows

1. Windows: установить Docker Desktop (WSL2 backend), запустить. Затем `docker version` и `npm run sandbox:build`.
2. `npm run test:sandbox`: 50 тестов OK, `docker ps -a --filter label=finlens.sandbox-job` пуст.
3. `set FINLENS_QWEN_PROVIDER=fake` (или production `FINLENS_QWEN_*`), затем `npm run dev:api`; во втором терминале `npm run dev`; sideload `manifest.xml` (как в `2026-10-14-1650-...md`).
4. Открыть копию обезличенной P&L-книги, сохранить SHA-256 (`certutil -hashfile book.xlsx SHA256`).
5. Выделить таблицу, спросить «Посчитай [fake:compute]» (или реальный вопрос с расчётом для Qwen):
   - ответ, «Источники», ID анализа, плашка «Расчёт выполнен…»;
   - «Методика», «Таблицы» и «Код» свёрнуты; график по ширине панели с текстом «Метрика / Период / Ключевой вывод».
6. «Копировать код» → вставить в Блокнот: совпадает с блоком «Код».
7. Клик по графику → увеличенный просмотр; Tab не уходит из диалога; Esc закрывает, фокус возвращается на график. Повторить только с клавиатуры и с экранным диктором.
8. «[fake:retry]» → «с 2-й попытки». «[fake:fail]» → «Не удалось ответить… Попыток расчёта: 3 из 3», без кода, таблиц и графика.
9. Остановить Docker Desktop и задать вопрос с `[fake:compute]` → «Изолированная песочница… недоступна». В диспетчере задач нет python-процесса, выполняющего код на host.
10. Логи API: только `analysisId`, коды, длительности и размеры. Ячеек, вопроса, кода и PNG нет.
11. Закрыть книгу без сохранения, сравнить SHA-256 — книга не изменена.
12. С production Qwen замерить время ответа с расчётом (ориентир ≤ 5 мин).

## Точный следующий шаг

Независимый code review Stories 2.1–2.3 (особенно `adapters/sandbox.py`, `code_policy.py`, `finlens_runner.py`). Затем:

- решение инфраструктуры по изоляции Docker (rootless / gVisor / отдельный хост);
- проверка с production Qwen;
- ручной тест на Windows.

Epic 2 не переводить в `done` до этого. Epic 3 не начат.

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
