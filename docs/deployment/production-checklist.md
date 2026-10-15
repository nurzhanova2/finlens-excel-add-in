# FinLens — production deployment checklist

[English](#english) · [Русский](#русский)

Status: **not deployed and not validated in production.** This checklist describes what an
infrastructure owner must decide and verify before FinLens is used outside a developer machine.
Nothing here has been executed against production infrastructure yet.

---

## English

### 1. Hard constraint: one API process per session (single instance / sticky routing)

The FinLens API keeps all live state **in the memory of one Python process**:

- conversation sessions (`SessionStore`: dialog history, workbook map, pending workbook plans);
- the analysis job registry (`AnalysisJobs`: stage, cancel signal, result kept for 300 s);
- the sandbox concurrency slots (`FINLENS_SANDBOX_MAX_CONCURRENT`).

There is no database, cache or shared store by design. Consequences:

- Run the API as **exactly one process** (`uvicorn` without `--workers N`, one replica), **or**
  route every request of a task pane to the same process (sticky routing by `sessionId` and
  `analysisId`; a session cookie cannot be used because the API sets none).
- A request that lands on another process gets `SESSION_NOT_FOUND` or `ANALYSIS_NOT_FOUND`;
  the pane then starts a new session and the dialog history is lost.
- A restart or redeploy of the API drops all sessions and running analyses. Running sandbox
  containers are force-removed only by the process that started them; check for leftovers
  (`docker ps -a --filter label=finlens.sandbox-job`) after a crash.
- Horizontal scaling therefore needs **either** sticky routing **or** a shared *ephemeral*
  job/session store (in-memory only, TTL-bound, no persistence). A shared store is **not
  implemented** and needs a separate architecture decision; do not add Redis, a database or
  cloud resources without that decision.

### 2. Network and origin

- [ ] Task pane static files and `/api` are served from **one HTTPS origin** through a corporate
      reverse proxy (no CORS: the API intentionally sends no CORS headers).
- [ ] The proxy forwards only `/api/*` to the API; the API itself listens on loopback or a
      private interface only, never directly on a user-reachable network.
- [ ] `manifest.xml` URLs (`https://localhost:5173/...` in dev) are replaced by the production
      origin; the manifest is distributed through the organisation's add-in catalogue.
- [ ] The production TLS certificate is issued by a CA trusted on analysts' Windows machines.
- [ ] Reachability is limited to the corporate network (there is no end-user login).
- [ ] Proxy timeouts allow short polling requests (status every 0.5 s) and the synchronous
      cancel request (up to ~20 s). No long-lived connections are needed.
- [ ] Rate limits account for polling: about 2 requests/s per running analysis.

### 3. Sandbox isolation (generated Python code)

- [ ] The sandbox image `finlens-sandbox:1` is built from `sandbox-image/` and available locally
      (`--pull never`).
- [ ] Docker runs **rootless**, or the OCI runtime is **gVisor** (`FINLENS_SANDBOX_OCI_RUNTIME=runsc`),
      or containers run on a **separate sandbox host**.
- [ ] **The Docker daemon is not reachable by the main API process without isolation.** Access to
      a rootful Docker socket is equivalent to root on that host. Acceptable options: rootless
      Docker for a dedicated service user; a dedicated sandbox host reached via `DOCKER_HOST`
      (TLS or SSH) with no other workloads; or an equivalent runner service decided by the
      infrastructure owner.
- [ ] No `docker.sock` is mounted into any container; sandbox flags are unchanged
      (`--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, non-root uid 10001,
      no mounts or env, CPU/memory/pids/time limits).
- [ ] `npm run test:sandbox` passes on the target host (no skips) and leaves no containers.

### 4. Qwen credentials and endpoint

- [ ] `FINLENS_QWEN_BASE_URL`, `FINLENS_QWEN_API_KEY`, `FINLENS_QWEN_MODEL` come from a
      **server-side secret store** (environment injected at start), never from the repository,
      the manifest, the task pane bundle or a shared `.env` file.
- [ ] `FINLENS_QWEN_PROVIDER` is **unset** in production (`fake` is a test provider).
- [ ] The Qwen endpoint is reachable directly from the API host. The adapter uses `http.client`
      and does **not** read `HTTP(S)_PROXY` environment variables and does not follow redirects.
- [ ] Use `https://` unless the endpoint is on a trusted internal segment; URLs with embedded
      credentials are rejected (`QWEN_NOT_CONFIGURED`).
- [ ] The endpoint supports OpenAI-compatible `tools`/`tool_choice` and `response_format: json_object`.
- [ ] Cancellation closes the HTTP connection; confirm the Qwen server stops (or tolerates) work on
      client disconnect.

### 5. Limits, monitoring, cleanup, audit metadata

- [ ] Limits reviewed against representative de-identified workbooks:
      `FINLENS_ANALYSIS_TIMEOUT_SECONDS` (600), `FINLENS_MAX_ACTIVE_ANALYSES` (8),
      `FINLENS_SESSION_TTL_SECONDS` (1800), `FINLENS_SANDBOX_TIMEOUT_SECONDS` (30),
      `FINLENS_SANDBOX_MEMORY_MB` (512), `FINLENS_SANDBOX_MAX_CONCURRENT` (2); fixed limits:
      10 000 cells per range, 20 000 cells and 5 ranges per workbook answer, 100 sheets, 4 000-character question.
- [ ] Logs are collected centrally. They contain only metadata (`analysisId`, codes, durations,
      sizes, attempt numbers). Verify that no cell values, prompts, code or PNGs appear.
- [ ] Alerts on: `SANDBOX_UNAVAILABLE`, `QWEN_UNAVAILABLE`, `ANALYSIS_CAPACITY`,
      `SESSION_CAPACITY`, `ANALYSIS_TIMEOUT`, `model_call_abandoned`, `analysis_cleanup_failed`.
- [ ] Periodic check that no `finlens.sandbox-job` containers outlive their analysis.
- [ ] Audit metadata retention defined by the security owner (what metadata, how long, who reads it).
- [ ] Health/liveness probe chosen (there is no dedicated health endpoint yet; `POST /api/sessions`
      creates state and should not be used as a probe).

### 6. Read-only guarantee

- [ ] The deployed bundle is built from a commit where `npm test` (static guards: no Office.js write
      API, no browser storage, no client secrets) passed.
- [ ] Byte-for-byte comparison of a test workbook before/after a pilot session (see the manual
      acceptance checklist).

---

## Русский

### 1. Жёсткое ограничение: один процесс API на сеанс (single instance / sticky routing)

API FinLens хранит всё живое состояние **в памяти одного процесса Python**:

- сеансы диалога (`SessionStore`: история, карта книги, отложенные планы чтения книги);
- реестр анализов (`AnalysisJobs`: этап, сигнал отмены, результат хранится 300 с);
- слоты параллельных запусков песочницы (`FINLENS_SANDBOX_MAX_CONCURRENT`).

Базы данных, кэша и общего хранилища нет намеренно. Следствия:

- Запускайте API **ровно одним процессом** (`uvicorn` без `--workers N`, одна реплика) **или**
  направляйте все запросы одной панели в один и тот же процесс (sticky routing по `sessionId` и
  `analysisId`; cookie не подойдёт — API их не выставляет).
- Запрос, попавший в другой процесс, получит `SESSION_NOT_FOUND` или `ANALYSIS_NOT_FOUND`;
  панель начнёт новый сеанс, история диалога потеряется.
- Перезапуск или передеплой API удаляет все сеансы и идущие анализы. Контейнеры песочницы
  принудительно удаляет только процесс, который их запустил; после аварии проверьте остатки
  (`docker ps -a --filter label=finlens.sandbox-job`).
- Горизонтальное масштабирование требует **либо** sticky routing, **либо** общего *эфемерного*
  хранилища задач и сеансов (только в памяти, с TTL, без персистентности). Общее хранилище
  **не реализовано** и требует отдельного архитектурного решения; не добавляйте Redis, базу
  данных или облачные ресурсы без такого решения.

### 2. Сеть и origin

- [ ] Статика панели и `/api` отдаются с **одного HTTPS-origin** через корпоративный reverse proxy
      (без CORS: API намеренно не отправляет CORS-заголовки).
- [ ] Прокси пересылает на API только `/api/*`; сам API слушает только loopback или приватный
      интерфейс и никогда не доступен пользователям напрямую.
- [ ] URL в `manifest.xml` (в dev — `https://localhost:5173/...`) заменены на production-origin;
      manifest распространяется через корпоративный каталог надстроек.
- [ ] Production TLS-сертификат выпущен CA, которому доверяют Windows-машины аналитиков.
- [ ] Доступ ограничен корпоративной сетью (входа пользователя нет).
- [ ] Таймауты прокси допускают короткие запросы опроса (каждые 0,5 с) и синхронный запрос
      отмены (до ~20 с). Долгих соединений нет.
- [ ] Rate limit учитывает опрос: около 2 запросов/с на один идущий анализ.

### 3. Изоляция песочницы (сгенерированный Python-код)

- [ ] Образ `finlens-sandbox:1` собран из `sandbox-image/` и доступен локально (`--pull never`).
- [ ] Docker работает в режиме **rootless**, или OCI runtime — **gVisor** (`FINLENS_SANDBOX_OCI_RUNTIME=runsc`),
      или контейнеры запускаются на **отдельном sandbox-хосте**.
- [ ] **Docker daemon недоступен основному процессу API без изоляции.** Доступ к rootful
      Docker-сокету равен root на этом хосте. Допустимо: rootless Docker под выделенным
      сервисным пользователем; отдельный sandbox-хост через `DOCKER_HOST` (TLS или SSH) без
      других нагрузок; эквивалентный runner-сервис по решению владельца инфраструктуры.
- [ ] `docker.sock` не монтируется ни в один контейнер; флаги песочницы не изменены
      (`--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, uid 10001,
      без mounts и env, лимиты CPU/памяти/pids/времени).
- [ ] `npm run test:sandbox` проходит на целевом хосте без пропусков и не оставляет контейнеров.

### 4. Учётные данные и endpoint Qwen

- [ ] `FINLENS_QWEN_BASE_URL`, `FINLENS_QWEN_API_KEY`, `FINLENS_QWEN_MODEL` берутся из
      **серверного хранилища секретов** (переменные окружения при старте), никогда — из
      репозитория, manifest, бандла панели или общего `.env`.
- [ ] `FINLENS_QWEN_PROVIDER` в production **не задан** (`fake` — тестовый провайдер).
- [ ] Endpoint Qwen доступен с хоста API напрямую. Адаптер использует `http.client`,
      **не** читает переменные `HTTP(S)_PROXY` и не следует редиректам.
- [ ] Используйте `https://`, если endpoint не находится в доверенном внутреннем сегменте;
      URL со встроенными учётными данными отклоняется (`QWEN_NOT_CONFIGURED`).
- [ ] Endpoint поддерживает OpenAI-совместимые `tools`/`tool_choice` и `response_format: json_object`.
- [ ] Отмена закрывает HTTP-соединение; проверьте, что сервер Qwen прекращает (или спокойно
      переносит) работу при разрыве соединения клиентом.

### 5. Лимиты, мониторинг, очистка, audit-метаданные

- [ ] Лимиты проверены на типичных обезличенных книгах:
      `FINLENS_ANALYSIS_TIMEOUT_SECONDS` (600), `FINLENS_MAX_ACTIVE_ANALYSES` (8),
      `FINLENS_SESSION_TTL_SECONDS` (1800), `FINLENS_SANDBOX_TIMEOUT_SECONDS` (30),
      `FINLENS_SANDBOX_MEMORY_MB` (512), `FINLENS_SANDBOX_MAX_CONCURRENT` (2); фиксированные:
      10 000 ячеек на диапазон, 20 000 ячеек и 5 диапазонов на ответ по книге, 100 листов, вопрос до 4 000 символов.
- [ ] Логи собираются централизованно и содержат только метаданные (`analysisId`, коды,
      длительности, размеры, номера попыток). Проверено, что значений ячеек, промптов, кода и PNG нет.
- [ ] Алерты на: `SANDBOX_UNAVAILABLE`, `QWEN_UNAVAILABLE`, `ANALYSIS_CAPACITY`,
      `SESSION_CAPACITY`, `ANALYSIS_TIMEOUT`, `model_call_abandoned`, `analysis_cleanup_failed`.
- [ ] Периодическая проверка, что контейнеры `finlens.sandbox-job` не переживают свой анализ.
- [ ] Владелец безопасности определил хранение audit-метаданных (какие, сколько, кто читает).
- [ ] Выбрана проба health/liveness (отдельного health-endpoint пока нет; `POST /api/sessions`
      создаёт состояние и не подходит как проба).

### 6. Гарантия read-only

- [ ] Развёрнутый бандл собран из коммита, на котором прошёл `npm test` (статические проверки:
      нет записи через Office.js, нет browser storage, нет секретов в клиенте).
- [ ] Побайтовое сравнение тестовой книги до и после пилотного сеанса (см. ручной acceptance checklist).
