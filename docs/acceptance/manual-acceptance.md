# FinLens — manual acceptance checklist

[English](#english) · [Русский](#русский)

**Status: not executed.** None of the checks below has been performed yet. The pane UI is in
Russian; the English section quotes the Russian labels with a translation. Automated tests ran on
macOS with Docker Desktop and a fake Qwen provider only. Record each run in the table at the end.

---

## English

### 0. Preparation

- [ ] Windows 10/11 machine with Excel Desktop (Microsoft 365), Docker Desktop (WSL2) or access to the target sandbox host, Node.js, Python 3.11+.
- [ ] A **de-identified** test workbook: one P&L sheet with two years, plus a multi-sheet model (3+ sheets).
- [ ] Save a copy of each workbook and record its hash: `certutil -hashfile book.xlsx SHA256`.
- [ ] `npm ci`, `python -m pip install -r api/requirements.txt`, `npm run dev:certs` (accept the CA), `npm run sandbox:build`.
- [ ] Note the commit hash under test.

### A. Excel Desktop for Windows

1. [ ] Start `npm run dev:api` and `npm run dev`; open `https://localhost:5173/index.html` in Edge — no certificate warning.
2. [ ] Sideload `manifest.xml` (`npx office-addin-debugging start manifest.xml desktop` or a shared-folder catalogue).
3. [ ] Home → **«Открыть FinLens»** (Open FinLens): the pane opens without «Эта версия Excel не поддерживается» (unsupported Excel); the context badge shows `Выделение · Лист · адрес` (Selection · sheet · address).
4. [ ] Select a P&L range and ask a comparison question: stages appear, then the answer with conclusion, evidence, risks/assumptions, next check, sources and analysis ID.
5. [ ] `/sheet`, then `/workbook`: the history is cleared on each scope change; workbook mode shows «Строю карту книги…» (building the workbook map), «Выбираю релевантные листы и диапазоны…» (choosing relevant sheets and ranges), «Читаю только выбранные диапазоны: …» (reading only the selected ranges) and the partial-coverage note «Проанализирована только часть книги…».
6. [ ] Ask a follow-up: two turns in the history, the workbook map is not rebuilt.
7. [ ] Empty selection → «…нет анализируемых данных…» (no analysable data) + button «Выбрать другой контекст» (choose another context); a selection over 10 000 cells → sending is blocked with the limit explained.
8. [ ] DevTools of the pane: all requests go to `https://<origin>/api/...`, no CORS or mixed-content errors; no `localStorage`/`sessionStorage` entries.
9. [ ] Close and reopen the pane: history is empty.

### B. Production Qwen

1. [ ] API started with `FINLENS_QWEN_BASE_URL`, `FINLENS_QWEN_API_KEY`, `FINLENS_QWEN_MODEL` from the secret store; `FINLENS_QWEN_PROVIDER` unset.
2. [ ] Simple range question: useful answer in the question's language; time ≤ 1 min (record it).
3. [ ] Question needing a calculation: executed code, table and PNG appear; numbers match a reference calculation of the test workbook; time recorded (target ≤ 5 min).
4. [ ] Workbook question: selected ranges are relevant; no more than 5 ranges / 20 000 cells.
5. [ ] Question the data cannot answer: block «Данных недостаточно для обоснованного вывода» (not enough data) with the missing data listed, heading «Вывод (не подтверждён)» (conclusion, not confirmed).
6. [ ] Cancel during «Готовлю вывод…» (preparing the answer): «Анализ отменён» (analysis cancelled) within ~2 s; API log shows `model_call_aborted`; Qwen server logs show the client disconnect; a new question then works.
7. [ ] Wrong API key / unreachable endpoint: typed error with one next step, no secrets or URLs on screen or in logs.

### C. Screen reader (NVDA or Windows Narrator) and keyboard only

1. [ ] Steps A.4–A.7 with keyboard only: Tab / Shift+Tab follow the visual order (scope buttons → results → «Начать новый диалог» → error panel → question → «Отправить вопрос» → «Отменить анализ»); focus is always visible.
2. [ ] Enter sends, Shift+Enter makes a new line, `/range` `/sheet` `/workbook` + Enter switch context.
3. [ ] Announced: «Этап: …» (stage), «Готово: …» (done), «Ошибка: … Следующий шаг: …» (error, next step), «Отменено: …» (cancelled); the elapsed timer is not announced on every poll.
4. [ ] While analysing, focus moves to «Отменить анализ» (cancel analysis); Esc cancels; after cancel focus returns to the question.
5. [ ] Chart: Enter opens the enlarged view, focus on «Закрыть просмотр (Esc)» (close), Tab stays inside, Esc closes and returns focus to the chart button; chart caption (metric, period, key insight) is read.
6. [ ] «Копировать источники» / «Копировать код» (copy sources / code): paste into Notepad matches the screen text.

### D. Docker sandbox

1. [ ] `npm run test:sandbox` passes with no skips; afterwards `docker ps -a --filter label=finlens.sandbox-job` is empty.
2. [ ] With `FINLENS_QWEN_PROVIDER=fake`: `[fake:compute]` → table + PNG; `[fake:retry]` → «с 2-й попытки» (on attempt 2); `[fake:fail]` → honest failure «Попыток расчёта: 3 из 3», no code/table/chart.
3. [ ] `[fake:long]`: while «Выполняю расчёт в изолированной песочнице…» a container is listed; cancel → the container disappears in ~1 s.
4. [ ] Docker stopped → «Изолированная песочница… недоступна» (sandbox unavailable), next step «Сообщите администратору…» (tell the administrator), no Python process on the host runs the code.
5. [ ] Target host: rootless Docker, gVisor or separate sandbox host in place (see the production checklist).

### E. Workbook unchanged

1. [ ] After every scenario above: close the workbook **without saving**.
2. [ ] `certutil -hashfile book.xlsx SHA256` equals the hash recorded in step 0.
3. [ ] Excel never asked to save changes after a FinLens-only session (if it did, record what was done).

---

## Русский

### 0. Подготовка

- [ ] Windows 10/11 с Excel Desktop (Microsoft 365), Docker Desktop (WSL2) или доступ к целевому sandbox-хосту, Node.js, Python 3.11+.
- [ ] **Обезличенная** тестовая книга: лист P&L за два года и многолистовая модель (от 3 листов).
- [ ] Сохраните копию каждой книги и её хэш: `certutil -hashfile book.xlsx SHA256`.
- [ ] `npm ci`, `python -m pip install -r api/requirements.txt`, `npm run dev:certs` (подтвердить CA), `npm run sandbox:build`.
- [ ] Запишите проверяемый commit.

### A. Excel Desktop для Windows

1. [ ] Запустить `npm run dev:api` и `npm run dev`; открыть `https://localhost:5173/index.html` в Edge — без предупреждения о сертификате.
2. [ ] Sideload `manifest.xml` (`npx office-addin-debugging start manifest.xml desktop` или каталог в общей папке).
3. [ ] «Главная» → **«Открыть FinLens»**: панель открывается без «не поддерживается»; плашка контекста показывает `Выделение · Лист · адрес`.
4. [ ] Выделить P&L и задать вопрос о сравнении годов: видны этапы, затем ответ — вывод, доказательства, риски и допущения, следующая проверка, источники, ID анализа.
5. [ ] `/sheet`, затем `/workbook`: при каждой смене контекста история очищается; в режиме книги видны «Строю карту книги…», «Выбираю релевантные…», «Читаю только выбранные диапазоны (…)» и пометка о частичном покрытии.
6. [ ] Уточняющий вопрос: в истории два хода, карта книги не перестраивается.
7. [ ] Пустое выделение → «нет анализируемых данных» + «Выбрать другой контекст»; выделение больше 10 000 ячеек → отправка заблокирована с объяснением лимита.
8. [ ] DevTools панели: все запросы идут на `https://<origin>/api/...`, ошибок CORS и mixed content нет; записей `localStorage`/`sessionStorage` нет.
9. [ ] Закрыть и снова открыть панель: история пустая.

### B. Production Qwen

1. [ ] API запущен с `FINLENS_QWEN_BASE_URL`, `FINLENS_QWEN_API_KEY`, `FINLENS_QWEN_MODEL` из хранилища секретов; `FINLENS_QWEN_PROVIDER` не задан.
2. [ ] Простой вопрос по диапазону: полезный ответ на языке вопроса; время ≤ 1 мин (записать).
3. [ ] Вопрос с расчётом: видны выполненный код, таблица и PNG; числа совпадают с эталонным расчётом; время записано (ориентир ≤ 5 мин).
4. [ ] Вопрос по книге: выбранные диапазоны релевантны; не больше 5 диапазонов и 20 000 ячеек.
5. [ ] Вопрос, на который данных не хватает: блок «Данных недостаточно…» со списком недостающего, «Вывод (не подтверждён)».
6. [ ] Отмена на этапе «Готовлю вывод…»: «Анализ отменён» за ~2 с; в логе API `model_call_aborted`; в логах Qwen виден разрыв соединения клиентом; новый вопрос после этого работает.
7. [ ] Неверный ключ или недоступный endpoint: типизированная ошибка с одним следующим шагом, без секретов и URL на экране и в логах.

### C. Экранный диктор (NVDA или Windows Narrator) и только клавиатура

1. [ ] Шаги A.4–A.7 только с клавиатуры: Tab / Shift+Tab идут в визуальном порядке (кнопки контекста → результаты → «Начать новый диалог» → панель ошибки → поле вопроса → «Отправить» → «Отменить»); фокус всегда виден.
2. [ ] Enter отправляет, Shift+Enter переносит строку, `/range` `/sheet` `/workbook` + Enter меняют контекст.
3. [ ] Озвучиваются «Этап: …», «Готово: …», «Ошибка: … Следующий шаг: …», «Отменено: …»; счётчик времени не озвучивается на каждом опросе.
4. [ ] Во время анализа фокус на «Отменить анализ»; Esc отменяет; после отмены фокус в поле вопроса.
5. [ ] График: Enter открывает увеличенный просмотр, фокус на «Закрыть», Tab не выходит из диалога, Esc закрывает и возвращает фокус на кнопку графика; подпись (метрика, период, ключевой вывод) читается.
6. [ ] «Копировать источники» / «Копировать код»: вставка в Блокнот совпадает с текстом на экране.

### D. Docker-песочница

1. [ ] `npm run test:sandbox` проходит без пропусков; после него `docker ps -a --filter label=finlens.sandbox-job` пуст.
2. [ ] С `FINLENS_QWEN_PROVIDER=fake`: `[fake:compute]` → таблица и PNG; `[fake:retry]` → «со 2-й попытки»; `[fake:fail]` → честная ошибка «3 из 3», без кода, таблиц и графика.
3. [ ] `[fake:long]`: во время «Выполняю расчёт…» контейнер виден; отмена → контейнер исчезает за ~1 с.
4. [ ] Docker остановлен → «песочница недоступна», следующий шаг «сообщите администратору»; на host нет Python-процесса, выполняющего код.
5. [ ] Целевой хост: rootless Docker, gVisor или отдельный sandbox-хост (см. production checklist).

### E. Неизменность книги

1. [ ] После каждого сценария выше закрыть книгу **без сохранения**.
2. [ ] `certutil -hashfile book.xlsx SHA256` совпадает с хэшем из шага 0.
3. [ ] После сеанса только с FinLens Excel не спрашивал «Сохранить изменения?» (если спросил — записать, что делалось).

---

## Run log / Журнал прогонов

| Date / Дата | Tester / Тестировщик | Commit | Section / Раздел | Excel build / Сборка Excel | Result / Результат | Notes / Заметки |
| --- | --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | not executed / не выполнялось | — |
