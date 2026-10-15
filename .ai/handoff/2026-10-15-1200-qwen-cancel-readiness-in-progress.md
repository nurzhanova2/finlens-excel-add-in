# Отмена Qwen на уровне HTTP + production-границы + README — in progress

## Контекст

Продолжение после `2026-10-15-1100-epic-3-progress-cancel-errors-a11y.md`. Цель: закрыть gap «отмена не прерывает HTTP-запрос Qwen», задокументировать production-границы и ручные проверки, переписать README на EN/RU.

## Сделано (шаг 1 — код отмены Qwen)

- Новый `api/app/domain/cancellation.py`: `CancelSignal` (подкласс `threading.Event` с callback-ами при `set()`), `CallCancelled`.
- `api/app/adapters/qwen.py`:
  - `QwenProvider.next_turn/select_ranges` принимают `cancel: CancelSignal | None`;
  - `HttpQwenProvider` переведён с `urlopen` на `http.client` (stdlib, без новых зависимостей). При отмене callback делает `shutdown(SHUT_RDWR)` сокета → блокирующее чтение сразу возвращается → `CallCancelled`;
  - таймаут сокета 55 с (`timeout_seconds`), ошибки по-прежнему `QWEN_UNAVAILABLE`/`QWEN_INVALID_RESPONSE`; non-2xx → `QWEN_UNAVAILABLE`; URL с другой схемой или credentials → `QWEN_NOT_CONFIGURED`;
  - fake `[fake:slow]` ждёт `cancel.wait()` вместо `sleep`.
- `api/app/application/lifecycle.py`: `cancel_event` теперь `CancelSignal`; `control.call` передаёт `cancel=`; после отмены воркер ждёт до 2 с (`MODEL_CALL_ABORT_GRACE_SECONDS`), пока HTTP-вызов действительно завершится, и только потом публикует `cancelled`. Лог `model_call_aborted analysisId=… unwindMs=…` или `model_call_abandoned` (если провайдер игнорирует сигнал).
- Тесты: новый `api/tests/test_qwen_cancellation.py` (12 тестов, реальный локальный HTTP fake Qwen с удержанием запроса), `support.FakeQwenServer`; старые HTTP-тесты переведены с патча `urlopen` на этот сервер; фейки принимают `cancel`.
- `npm run test:api` — 110 OK (skipped=8 — Docker-тесты без флага обязательности).

## Следующий шаг

Документация: production deployment checklist, ручной acceptance checklist, двуязычный README. Затем полный набор проверок и итоговый handoff.

## Обновление — документация готова

- `docs/deployment/production-checklist.md` (EN/RU): single-instance/sticky ограничение, единый HTTPS origin, изоляция Docker (rootless/gVisor/отдельный хост, daemon не доступен API без изоляции), секреты Qwen, лимиты, мониторинг, очистка, audit-метаданные.
- `docs/acceptance/manual-acceptance.md` (EN/RU): Excel Desktop Windows, production Qwen, NVDA/Narrator, Docker sandbox, неизменность `.xlsx`; статус «не выполнялось», журнал прогонов.
- `README.md` переписан: равноправные разделы English / Русский (9 пунктов каждый).
- `api/app/main.py`: комментарий о single-process состоянии.

## Следующий шаг

Полный набор проверок, итоговый handoff.
