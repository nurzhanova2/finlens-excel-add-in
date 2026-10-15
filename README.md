<div align="center">

# FinLens

### A read-only AI financial analyst inside the Excel task pane

**Language:** [Русский](README_ru.md) · [English](README.md)

[![Status — MVP, pilot validation pending](https://img.shields.io/badge/status-MVP%2C%20pilot%20validation%20pending-f9a825)](docs/acceptance/manual-acceptance.md)
[![Platform — Excel Desktop for Windows](https://img.shields.io/badge/platform-Excel%20Desktop%20for%20Windows-1565c0)](manifest.xml)
[![Workbook — read-only](https://img.shields.io/badge/workbook-read--only-2e7d32)](#product-boundaries)
[![Calculations — isolated Docker sandbox](https://img.shields.io/badge/calculations-isolated%20Docker%20sandbox-6a1b9a)](sandbox-image/Dockerfile)

</div>

> [!IMPORTANT]
> FinLens never changes the workbook. It reads only the context you choose, sends it to the internal
> Qwen model through the FinLens API, and shows the answer in the task pane. Nothing is inserted into
> the workbook and nothing is stored after the session.

## Why this project

Financial analysts spend a lot of time understanding unfamiliar models: where the drivers are, why a
line moved, whether totals add up, which assumptions look aggressive. FinLens lets an analyst ask
those questions in plain language, right next to the data:

```text
choose context → read data (read-only) → ask a question → model reasons → optional Python calculation → verifiable answer
```

Every answer shows its sources (exact sheets and ranges). When a calculation is needed, FinLens runs
Python code in an isolated sandbox and shows the exact code, result tables and chart — so the analyst
can check the numbers instead of trusting a summary.

## What it can do

| Capability | Details |
|---|---|
| Choose the context | Selected range, active sheet or whole workbook; a badge shows exactly what will be read |
| Ask in plain language | Russian or English; the answer comes in the language of the question |
| Analyst-style answer | Conclusion, evidence, risks and assumptions, next check, sources, analysis ID |
| Large workbooks | Builds a map of the workbook, then reads only the relevant ranges and says so |
| Calculations | `pandas` / `numpy` / `matplotlib` in a throw-away container; up to 2 automatic fixes |
| Verifiable results | Method, result tables, executed code, PNG charts with metric, period and key insight |
| Follow-up questions | The dialog keeps context while the pane is open |
| Progress and cancel | Stage shown in words; cancel stops the model request and the sandbox job |
| Honest limits | "Not enough data" answers and typed errors with one clear next step |
| Accessibility | Full keyboard control, screen-reader announcements, text instead of colour-only signals |

## Core rule

> [!WARNING]
> The model can be wrong. A conclusion is only as good as the data and the calculation behind it.
> Check the sources, the method and the executed code before using a result in a decision.

FinLens enforces part of this itself: a computed claim is shown only if the calculation actually
succeeded; after three failed attempts the answer is an honest error, not a guessed number.

## The task pane

The pane interface is in Russian; labels are given with an English translation.

```text
┌──────────────────────────────────────────────┐
│ FinLens                                      │  title
│ Read-only аналитик для Excel                 │  read-only promise
│ [Выделение · P&L · A1:F40]                   │  context badge
│ (Выделение) (Лист) (Книга)                   │  Range / Sheet / Workbook
├──────────────────────────────────────────────┤
│ Вопрос 1 · Выделение: Why did margin fall?   │  dialog history
│   Вывод / Доказательства / Риски и допущения │  answer
│   Следующая проверка / Источники / ID        │
│   ▸ Методика ▸ Таблицы ▸ Код ▾ График [PNG]  │  verifiable calculation
│ (Начать новый диалог)                        │  start a new dialog
├──────────────────────────────────────────────┤
│ Уточняющий вопрос                            │  question box
│ (Отправить вопрос)  (Отменить анализ)        │  send / cancel
│ Этап: Готовлю вывод…   Анализ идёт 12 с.     │  stage and elapsed time
└──────────────────────────────────────────────┘
```

| Element | What it does |
|---|---|
| «Выделение» / «Лист» / «Книга» | Range / active sheet / whole workbook. Changing the context starts a new dialog |
| Context badge | e.g. `Выделение · P&L · A1:F40` or `Книга · 6 лист(ов), с данными: 4` |
| Dialog | «Вопрос N · context» and the answer; follow-ups use earlier turns |
| «Источники» | Exact `sheet · range` lines; «Копировать источники» copies them |
| «Данных недостаточно…» | Shown when the data cannot support a conclusion; lists what is missing; heading becomes «Вывод (не подтверждён)» |
| Workbook coverage | «Проанализирована только часть книги… Это не анализ всей книги» with the ranges used |
| «Методика» / «Таблицы» / «Код» | Method and inputs, result tables, the code that actually ran; «Копировать код» copies it |
| «График» | PNG with metric, period and key insight; click or Enter to enlarge, Esc to close |
| «Этап: …» | Current stage in words; no spinner, colour only duplicates the text |
| «Отменить анализ» | Appears and takes focus while an analysis runs; Esc does the same |

The pane cannot jump to a source range: selecting cells would change the workbook state, which the
read-only rule forbids. Use the exact source text to find the range.

## Quick start for analysts

1. Open the workbook in Excel Desktop → Home → **«Открыть FinLens»**.
2. Select a range, or press «Лист» or «Книга». Check the context badge.
3. Type a question and press **Enter**.
4. Open «Методика», «Таблицы» or «Код» to verify the numbers; ask a follow-up if needed.

| Key / command | Effect |
|---|---|
| Enter | Send the question |
| Shift+Enter | New line |
| Tab / Shift+Tab | Move through context buttons → results → new dialog → error panel → question → send → cancel |
| Esc | Cancel the running analysis (or close an enlarged chart first) |
| `/range`, `/sheet`, `/workbook` + Enter | Switch context and start a new dialog |

**Good questions** name the metric, the period and, if known, the rows: *"Compare 2024 and 2025
revenue by segment and explain the main drivers"*. Ask for a calculation explicitly when you need
exact figures, and choose the smallest context that contains the answer.

| English | Русский |
|---|---|
| What changed between 2024 and 2025, and which lines drove the change? | Почему операционная прибыль снизилась, хотя выручка выросла? |
| Calculate the gross margin for each quarter and plot it. | Посчитай рост выручки год к году по сегментам и построй график. |
| Which forecast assumptions look aggressive compared with actuals? | Какие статьи затрат требуют проверки в первую очередь? |
| Are there rows where totals do not equal the sum of components? | Найди листы с бюджетом и фактом и сравни их. *(«Книга»)* |

## Statuses and errors

| Stage | Text in the pane | Meaning |
|---|---|---|
| `reading_context` | «Читаю выбранные данные…» | Reading the chosen data from Excel |
| `mapping_workbook` | «Строю карту книги…» | Listing sheets and sizes, no cell values |
| `selecting_artifacts` | «Выбираю релевантные листы и диапазоны…» | The model picks up to 5 relevant ranges |
| `preparing_answer` | «Готовлю вывод…» | The model is working on the answer |
| `running_computation` | «Выполняю расчёт в изолированной песочнице…» | Python runs in the sandbox |
| `retrying_computation` | «Исправляю расчёт (попытка N из 2)…» | Automatic correction after a failed run |
| `finalizing` | «Завершаю ответ…» | Saving the turn in this dialog |
| `cancelling` / `cancelled` | «Отменяю анализ…» / «Анализ отменён» | Stopped and cleaned up; ask a new question |
| `completed` | «Готово: ответ получен за N с.» | The answer is shown |

Each error appears in one panel with «Следующий шаг» (next step) and at most one button:
«Повторить» (retry), «Выбрать другой контекст» (choose another context) or «Изменить вопрос» (edit question).

| Code | What happened | What to do |
|---|---|---|
| `EMPTY_CONTEXT`, `CONTEXT_UNAVAILABLE` | No data in the context, or Excel did not return it | Select a range with data / reopen the workbook |
| `CONTEXT_TOO_LARGE`, `WORKBOOK_TOO_LARGE`, `WORKBOOK_SCOPE_TOO_LARGE` | Above 10 000 cells, 100 sheets, or 5 ranges / 20 000 cells | Narrow the context |
| `NO_RELEVANT_DATA`, `WORKBOOK_MAP_MISSING` | Nothing relevant found / map expired | Use «Лист» on the right sheet / choose «Книга» again |
| `QUESTION_EMPTY`, `QUESTION_TOO_LONG` | No question / over 4 000 characters | Edit the question |
| `QWEN_INVALID_RESPONSE`, `UNVERIFIED_COMPUTATION` | Unusable model reply / claim without a successful calculation | Rephrase; say which calculation you need |
| `COMPUTATION_FAILED` | 3 failed calculation attempts; no answer is invented | Narrow the data or rephrase |
| `QWEN_UNAVAILABLE`, `SANDBOX_BUSY`, `ANALYSIS_CAPACITY`, `SESSION_CAPACITY` | Service busy or not answering | Retry in a minute |
| `ANALYSIS_TIMEOUT` | The analysis exceeded the time limit | Select less data and retry |
| `NETWORK_UNAVAILABLE`, `API_UNAVAILABLE`, `STATUS_UNAVAILABLE`, `CANCEL_UNCONFIRMED` | Connection to the FinLens API lost | Check the corporate network and retry |
| `SESSION_NOT_FOUND`, `SESSION_SCOPE_MISMATCH`, `PLAN_NOT_FOUND`, `ARTIFACTS_MISMATCH`, `ANALYSIS_NOT_FOUND`, `ANALYSIS_CONFLICT`, `INTERNAL_ERROR` | Session expired or state out of date | Send the question again |
| `UNSUPPORTED_EXCEL` | Excel lacks ExcelApi 1.1 | Use or update Excel Desktop for Windows |
| `QWEN_NOT_CONFIGURED`, `SANDBOX_UNAVAILABLE` | FinLens is not configured or the sandbox is down | Tell the FinLens administrator |

Error texts never contain stack traces, file paths, keys or raw Excel/network exceptions.

## Product boundaries

- **Excel is never edited** — no cells, formulas, formatting, sheets, names or selection.
- **Nothing is inserted into the workbook** — charts, tables and calculations stay in the pane.
- **No export, no login, no saved history** — the dialog lives while the pane is open (server TTL 30 min by default).
- **No external data** — no market, macro or internet data; the sandbox has no network.
- **Workbook mode is selective** — at most 5 ranges and 20 000 cells per answer, stated explicitly in the answer.
- **Limits** — 10 000 cells per range or sheet, 100 sheets, 4 000 characters per question, 3 calculation attempts.

## Security and privacy

| Question | Answer |
|---|---|
| What leaves Excel? | Only the chosen context: copied values, formulas and number formats. In workbook mode, first only sheet names and sizes, then only the selected ranges |
| Where does it go? | To the internal FinLens API and the internal Qwen model. Nowhere else |
| What is stored? | Nothing. Dialog, workbook map, code, tables and charts are deleted on a new dialog, context change, pane close, cancel or session expiry |
| How does generated code run? | In a throw-away container: no network, no host files or secrets, no Excel access, non-root, read-only filesystem, CPU/memory/time limits. Code is checked before it runs; without the sandbox nothing runs |
| What does cancel do? | Closes the model request, removes the running container and discards the partial result |
| What is logged? | Technical metadata only: analysis ID, codes, durations, sizes |
| Where are credentials? | The Qwen key exists only on the server. The add-in has no secrets and no login; access is limited by the corporate network |

Office requires the `ReadWriteDocument` permission even to read a workbook. The read-only guarantee
is enforced in code: no Office.js write API is used, automated static checks forbid it, and the
acceptance test compares the file hash before and after a session.

## How it works

```text
Excel (Office.js, read-only)
   │  copied context
   ▼
Task pane (React)  ──  same HTTPS origin  ──▶  FinLens API (FastAPI)
                                                 ├── Qwen adapter ──▶ internal Qwen (OpenAI-compatible)
                                                 └── sandbox runner ─▶ docker run finlens-sandbox:1
                                                                        (--network none, --read-only,
                                                                         non-root, resource limits)
```

The API validates every request, every model tool call and every sandbox result. Analyses run
asynchronously; the pane polls `GET /api/analyses/{id}` and cancels with `POST /api/analyses/{id}/cancel`.

## Run locally

Requirements: Node.js 20.19+ or 22.12+, Python 3.11, Docker, and Excel Desktop for Windows for the
real add-in.

```bash
npm ci
python3 -m pip install -r api/requirements.txt
npm run dev:certs        # once: trusted localhost certificate for Office add-ins
npm run sandbox:build    # builds finlens-sandbox:1 (Docker must be running)

npm run dev:api          # terminal 1: API on 127.0.0.1:8000
npm run dev              # terminal 2: task pane on https://localhost:5173, /api proxied to the API
```

Sideload into Excel Desktop: `npx office-addin-debugging start manifest.xml desktop`, or add
`manifest.xml` to a shared-folder add-in catalogue. On Windows without `python3`, start the API with
`py -m uvicorn app.main:app --app-dir api --host 127.0.0.1 --port 8000`.

**Test provider.** `FINLENS_QWEN_PROVIDER=fake` runs a deterministic stand-in for Qwen that performs no
analysis. Markers in the question trigger scenarios: `[fake:compute]`, `[fake:retry]`,
`[fake:invalid-tool]`, `[fake:fail]`, `[fake:unverified]`, `[fake:slow]`, `[fake:long]`, `[fake:insufficient]`.

**Production Qwen** — set on the server only, from a secret store; never commit these values or put
them in the manifest or the task pane:

| Variable | Meaning |
|---|---|
| `FINLENS_QWEN_BASE_URL` | OpenAI-compatible base URL, e.g. `https://<internal-qwen-host>/v1` |
| `FINLENS_QWEN_API_KEY` | Server-held key |
| `FINLENS_QWEN_MODEL` | Model name (default `qwen`) |
| `FINLENS_QWEN_PROVIDER` | Leave unset in production |

<details>
<summary>Other settings</summary>

| Variable | Default | Allowed |
|---|---|---|
| `FINLENS_SESSION_TTL_SECONDS` | 1800 | 60–86400 |
| `FINLENS_ANALYSIS_TIMEOUT_SECONDS` | 600 | 60–3600 |
| `FINLENS_MAX_ACTIVE_ANALYSES` | 8 | 1–64 |
| `FINLENS_SANDBOX_RUNTIME` | `docker` | any other value disables calculations with an honest error |
| `FINLENS_SANDBOX_IMAGE` | `finlens-sandbox:1` | — |
| `FINLENS_SANDBOX_TIMEOUT_SECONDS` | 30 | 5–120 |
| `FINLENS_SANDBOX_MEMORY_MB` | 512 | 128–2048 |
| `FINLENS_SANDBOX_MAX_CONCURRENT` | 2 | 1–16 |
| `FINLENS_SANDBOX_OCI_RUNTIME` | — | e.g. `runsc` (gVisor) |
| `FINLENS_API_PROXY_TARGET` | `http://127.0.0.1:8000` | loopback only (dev server) |
| `FINLENS_DEV_CERTS_DIR` | `~/.office-addin-dev-certs` | — |

The Qwen endpoint must support `tools` / `tool_choice` and `response_format: json_object`. The
adapter does not use `HTTP(S)_PROXY` variables and does not follow redirects.

</details>

## Checks

```bash
npm test               # task pane tests, incl. static read-only / storage / secret guards
npm run typecheck
npm run lint
npm run build
npm run test:api       # backend tests; Docker tests are skipped without Docker
npm run test:sandbox   # sandbox security and cancellation suite; fails without Docker or the image
xmllint --noout manifest.xml
```

<details>
<summary>Static scans (bash; only the section headers should print)</summary>

```bash
SRC=$(find src -name '*.ts' -o -name '*.tsx' | grep -vE '\.test\.tsx?$|^src/test/')
echo "--office write"; grep -nE '\.(values|formulas|numberFormat|formulasLocal|formulasR1C1|name|visibility|position)\s*=[^=]|\.(insert|clear|delete|merge|unmerge|select|activate|calculate|copyFrom|moveTo|setValues|setFormulas|add)\s*\(' $SRC
echo "--storage"; grep -rnE 'localStorage|sessionStorage|indexedDB|document\.cookie' $SRC api/app config index.html
echo "--secrets"; grep -nE 'FINLENS_QWEN|API_KEY|Bearer\s' $SRC config/*.ts manifest.xml index.html
echo "--docker"; grep -rnE -- '--privileged|--cap-add|unconfined|--network[= ]host|docker\.sock|shell=True' api/app sandbox-image
```

</details>

## Structure

```text
.
├── src/taskpane/       # Excel task pane: context reading, dialog, results, accessibility
├── api/
│   ├── app/domain/     # contracts, limits, code policy, cancellation signal
│   ├── app/application/# sessions, workbook planning, agent loop, analysis lifecycle
│   ├── app/adapters/   # Qwen HTTP client, Docker sandbox runner
│   └── tests/          # unit, HTTP and real-Docker tests
├── sandbox-image/      # isolated Python image (pandas, numpy, matplotlib)
├── config/             # HTTPS dev server and /api proxy
├── docs/               # deployment and acceptance checklists
├── manifest.xml        # Office add-in manifest
└── index.html
```

## Validation status

| Area | Status |
|---|---|
| Automated tests (task pane, API, real Docker sandbox) | passing on macOS with Docker Desktop and the test provider |
| Excel Desktop for Windows | pending manual acceptance |
| Production Qwen (quality, latency, cancel on the real endpoint) | pending |
| Screen reader (NVDA / Windows Narrator) | pending |
| Sandbox on the target host (rootless Docker / gVisor / separate host) | pending |
| Workbook unchanged byte-for-byte after real sessions | pending |

Step-by-step checks: [manual acceptance checklist](docs/acceptance/manual-acceptance.md).

## Known limitations

- **One API process.** Sessions and running analyses live in memory of a single process. Run one
  process or route each pane to the same one; a restart clears all dialogs. See the
  [production deployment checklist](docs/deployment/production-checklist.md).
- **Excel Desktop for Windows** with ExcelApi 1.1 is the target; Excel on the web and Mac are not.
- **No jump to a source range** — it would change the selection.
- **Progress is polled** every 0.5 s rather than streamed.
- A cancel that arrives after the answer is committed is refused and the answer is shown with a note.
- Limits are starting values to be tuned on real de-identified workbooks.
