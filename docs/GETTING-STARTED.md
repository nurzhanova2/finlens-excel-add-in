# FinLens: guide for a new contributor

This guide is for a developer who has cloned the repository and needs to run, test, and understand FinLens safely.

## 1. What this project is

FinLens is an internal, read-only AI financial-analyst add-in for Excel Desktop on Windows. A user selects a range, sheet, or workbook and asks a free-form question. The task pane reads Excel data, the backend asks the internal Qwen service for an analysis, and—when needed—a sandbox executes limited Python calculations.

The product must never edit values, formulas, worksheets, charts, or workbook structure. It has no end-user authentication, no persistent chat history, no export, and no external market-data access.

## 2. Repository map

| Path | Purpose |
| --- | --- |
| `src/taskpane/` | React task pane, Office.js read-only extractor, API client, accessibility UI. |
| `api/app/` | FastAPI contracts, sessions, Qwen adapter, orchestration and sandbox adapter. |
| `api/tests/` | Backend, session, Qwen and sandbox tests. |
| `sandbox-image/` | Restricted Python Docker image and runner. |
| `manifest.xml` | Excel add-in manifest. |
| `.ai/handoff/` | Implementation decisions and verified handoffs. Read these before changes. |
| `docs/acceptance/` | External/manual acceptance procedures. |
| `docs/deployment/` | Production deployment constraints. |
| `_bmad-output/` | Retained PRD, UX, architecture and acceptance criteria. It is not runtime code. |

## 3. Prerequisites

- Node.js compatible with the repository lockfile and npm.
- Python 3.11 or newer.
- Docker Desktop for sandbox build/security tests.
- Excel Desktop for Windows for sideload and real add-in validation. macOS can run automated checks but cannot close that acceptance gate.
- An internal Qwen endpoint and credentials only for production-like testing. Never commit them.

## 4. Initial setup

From the project root:

```bash
npm ci
python3 -m pip install -r api/requirements.txt
```

Install or renew the local HTTPS development certificate when necessary:

```bash
npm run dev:certs
```

This changes the local machine's trust store and may request confirmation. Do not run it automatically on somebody else's machine.

Build the sandbox image after Docker Desktop is running:

```bash
npm run sandbox:build
```

## 5. Local development

Use two terminals.

Terminal 1 starts the API:

```bash
export FINLENS_QWEN_PROVIDER=fake
npm run dev:api
```

On Windows PowerShell use:

```powershell
$env:FINLENS_QWEN_PROVIDER = "fake"
npm run dev:api
```

Terminal 2 starts the HTTPS task pane:

```bash
npm run dev
```

The task pane is served from `https://localhost:5173`. Its `/api` requests are proxied only to the local API on `127.0.0.1:8000`; do not change this to a broad CORS configuration.

The fake provider is only for automated/local tests. A production-like run requires server-side `FINLENS_QWEN_BASE_URL`, `FINLENS_QWEN_API_KEY`, and optionally `FINLENS_QWEN_MODEL`. Keep all of them out of the browser, manifest, repository, screenshots, and logs.

## 6. Sideloading and manual acceptance

Sideload `manifest.xml` into Excel Desktop on Windows, open a de-identified copy of a workbook, and use the FinLens ribbon command. Follow the exact checklist in [manual acceptance](acceptance/manual-acceptance.md).

Before and after a manual session, compare the SHA-256 of the original `.xlsx`. The hashes must be equal. Test range, sheet, workbook, a calculation, cancellation, errors, sources, keyboard-only navigation, and a screen reader.

## 7. Commands to run before a review

```bash
npm test
npm run typecheck
npm run lint
npm run build
npm run test:api
npm run test:sandbox
xmllint --noout manifest.xml
```

`npm run test:sandbox` requires Docker Desktop and creates short-lived containers. Confirm none remain after a run:

```bash
docker ps -a --filter label=finlens.sandbox-job
```

For the full security and deployment expectations, read the latest handoffs and [production checklist](deployment/production-checklist.md).

## 8. Safety rules for any change

1. Do not call Office.js write APIs. FinLens is functionally read-only.
2. Do not send secrets or Qwen credentials to the task pane.
3. Do not add browser storage, persistent history, databases, or exports without an explicit product decision.
4. Do not execute generated Python on the host. If Docker or the sandbox image is unavailable, return a safe failure.
5. Preserve sandbox restrictions: non-root, no network, read-only filesystem, no host mounts, no secrets, resource limits, cleanup.
6. Log metadata only: IDs, durations, sizes and safe error codes. Never log workbook cells, prompts, code, PNGs or credentials.
7. Update `.ai/handoff/` after meaningful work so the next contributor can continue safely.

## 9. Current delivery status

The three planned epics are implemented but remain under review. External checks still required before a production claim:

- independent code/security review;
- production Qwen quality, tool-calling and latency evaluation;
- manual Excel Desktop on Windows sideload validation;
- NVDA or Windows Narrator accessibility validation;
- sandbox validation on the intended production infrastructure;
- deployment decision for sticky routing or a shared ephemeral job/session store, plus stronger Docker isolation.

Do not mark epics as done merely because automated tests pass.
