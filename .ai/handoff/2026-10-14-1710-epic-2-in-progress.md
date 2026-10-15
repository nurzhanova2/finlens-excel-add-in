# Epic 2 (Stories 2.1–2.3) — in progress

## Что сделано

- Прочитаны все handoff, Stories 2.1–2.3 в `epics.md`, Architecture Spine (AD-3/4/5/6), PRD, DESIGN, EXPERIENCE, sprint status, код `src/taskpane/`, `api/app/`, тесты, `package.json`, README.
- Docker CLI 29.1.3 установлен; daemon (Docker Desktop) не был запущен — запущен для интеграционных тестов песочницы.

## План

1. `sandbox-image/` — Dockerfile (python 3.12 slim, pinned pandas/numpy/matplotlib, uid 10001) и `finlens_runner.py` (stdin JSON → stdout JSON, ограниченные builtins/импорты, лимиты вывода, таймер, CPU rlimit).
2. Backend:
   - `domain/computation.py` — схема `run_analysis`, лимиты, таблицы/PNG/диагностика;
   - `domain/code_policy.py` — статическая AST-проверка кода и строк (импорты, URL, пути, секреты, subprocess/os/сеть, pandas IO);
   - `adapters/sandbox.py` — `DockerSandboxRunner` (`--network none`, `--read-only`, non-root, `--cap-drop ALL`, `no-new-privileges`, лимиты, `--rm` + `rm -f`), без fallback на host;
   - `application/agent.py` — цикл tool calling: ≤ 3 запусков (исходный + 2 исправления), безопасная диагностика, typed failure, запрет «расчётных» выводов без успешного execution artifact;
   - Qwen adapter — OpenAI-compatible `tools`/`tool_calls`; fake provider со сценариями-маркерами `[fake:compute|retry|fail|invalid-tool|unverified]`.
3. UI: блоки Method/Tables/Code/Chart, копирование кода, PNG с увеличением и Esc, описание графика.
4. Тесты, проверки, sprint status, итоговый handoff.

## Точный следующий шаг

Создать `sandbox-image/` и backend-домен.

## Обновление — backend и песочница готовы (Stories 2.1, 2.2)

- Добавлены `sandbox-image/` (Dockerfile, requirements, `finlens_runner.py`, `.dockerignore`), `api/app/domain/computation.py`, `api/app/domain/code_policy.py`, `api/app/adapters/sandbox.py`, `api/app/application/agent.py`, `api/app/application/errors.py`; переработаны `qwen.py` (tool calling, fake-сценарии), `analyze.py`, `workbook.py`, `main.py`, `contracts.py` (`ModelAnswer`, `computation`, `computationAttempts`).
- Образ `finlens-sandbox:1` собран локально (`docker build -t finlens-sandbox:1 sandbox-image`).
- Тесты: `test_tool_calls.py`, `test_agent.py`, `test_sandbox_runner.py` (поддельный docker CLI), `test_sandbox_docker.py` (реальный контейнер; 7/7 OK, оставшихся контейнеров 0). `npm run test:api` зелёный.
- Найдено и исправлено при реальном прогоне: SIGKILL от жёсткого CPU rlimit классифицировался как MEMORY_LIMIT.

## Точный следующий шаг

UI Story 2.3: контракты TS, валидация `computation`, блоки Method/Tables/Code/Chart, копирование, просмотр PNG с Esc; тесты.

## Итог

Epic 2 (Stories 2.1–2.3) реализован, Stories переведены в `review`; актуальное состояние — `2026-10-14-1745-epic-2-sandbox-retry-verifiable-results.md`.
