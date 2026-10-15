# Epic 1 blockers — Office.onReady и same-origin dev-контур

## Что сделано

- **Запуск add-in после `Office.onReady`.** Новый `src/taskpane/bootstrap.ts` (`waitForOffice`, `startTaskPane`). `main.tsx` монтирует React только после `Office.onReady()`, поэтому `getCompatibility()` видит уже инициализированный `Office.context`. Есть fallback: без Office.js (браузер, тесты) панель рендерится сразу. Если `onReady` отклонён, бросил исключение или не завершился за 10 с, панель тоже рендерится и показывает понятное состояние «не поддерживается», а не пустой экран.
- **Same-origin dev-контур без CORS и mixed content.**
  - Task pane и API для браузера — один HTTPS-origin `https://localhost:5173`. Vite проксирует только `/api` на API.
  - API слушает только `127.0.0.1:8000` (`npm run dev:api`).
  - Клиент использует относительный путь `/api` (`API_BASE_PATH`); `VITE_FINLENS_API_URL` и абсолютный `http://localhost:8000` удалены.
  - Новый `config/dev-server.ts`:
    - `host: localhost` (раньше `--host 0.0.0.0` открывал dev-сервер в сеть), `strictPort 5173`;
    - доверенные сертификаты `office-addin-dev-certs` с fail-fast, если их нет или срок истёк;
    - `cors: false`: Vite 7 по умолчанию выдавал `Access-Control-Allow-Origin` любым localhost-origin;
    - proxy-target разрешён только на loopback (`FINLENS_API_PROXY_TARGET`), без пути и credentials.
  - В API нет CORS middleware, и это закреплено тестом.
- `package.json`:
  - скрипты `dev` (`vite`, без `--host 0.0.0.0`), `dev:api`, `dev:certs`;
  - devDependencies `office-addin-dev-certs@3.0.1` и `@types/node@22.20.5` (pinned).
- `tsconfig.json` включает `config/`. README дополнен разделом «Локальный dev-контур».

## Изменённые файлы

- Новые: `src/taskpane/bootstrap.ts`, `src/taskpane/bootstrap.test.ts`, `config/dev-server.ts`, `config/dev-server.test.ts`, `api/tests/test_network.py`, этот handoff.
- Изменённые: `src/main.tsx`, `src/taskpane/office.ts` (`onReady` в `OfficeRuntime`), `src/taskpane/analysis-api.ts`, `src/taskpane/analysis-api.test.ts`, `vite.config.ts`, `tsconfig.json`, `package.json`, `package-lock.json`, `README.md`.
- Не менялись: manifest (уже `https://localhost:5173`), planning-артефакты, `sprint-status.yaml` (Stories/Epic в `done` не переводились).

## Тесты

- `bootstrap.test.ts`:
  - render не вызывается до `onReady`, а после него compatibility = `supported`;
  - fallback без Office;
  - timeout, rejected и sync-throw `onReady`.
- `config/dev-server.test.ts`:
  - HTTPS на localhost:5173, `cors: false`, proxy только `/api` → `http://127.0.0.1:8000`;
  - ошибка с инструкцией, если сертификатов нет или они истекли;
  - отказ для не-loopback proxy-target;
  - все URL manifest на origin task pane, внешние скрипты `index.html` только `https`, в `src` нет абсолютных URL на localhost/127.0.0.1.
- `analysis-api.test.ts`: запрос идёт на относительный `/api/analysis`.
- `api/tests/test_network.py`: нет `access-control-allow-origin` ни для какого origin (включая `https://localhost:5173`); CORS-preflight отклоняется (405).

## Выполненные проверки

- `npm test` — 7 файлов, 31 тест OK.
- `npm run typecheck`, `npm run lint`, `npm run build` — OK.
- `npm run test:api` — 21 тест OK; `python3 -m compileall -q api/app` — OK.
- `xmllint --noout manifest.xml` — OK.
- Read-only scan Office.js write API по `src/**/*.ts(x)` — совпадений нет.
- Scan task pane/config на секреты Qwen и браузерное хранилище — совпадений нет; в API нет `CORSMiddleware`.
- `git diff --check` и grep trailing whitespace — чисто.
- Живой smoke (macOS, fake provider, временный CA в scratchpad, затем удалён):
  - `https://localhost:5173/index.html` → 200, проверка сертификата прошла;
  - `POST https://localhost:5173/api/sessions` через proxy → 201;
  - ни для `https://localhost:5173`, ни для `http://localhost:3000`, ни для `https://evil.example` нет CORS-заголовков;
  - Vite слушает `127.0.0.1:5173`, API — `127.0.0.1:8766`.
- **Найдено:** dev-сертификаты на этой машине (`~/.office-addin-dev-certs`) истекли 2026-09-09. `npm run dev` теперь явно сообщает об этом. Сертификаты не переустанавливались: это изменение системного хранилища доверия, его выполняет пользователь (`npm run dev:certs`).

## Команды запуска

```bash
npm ci
python -m pip install -r api/requirements.txt
npm run dev:certs        # один раз или после истечения срока; подтвердить установку CA
npm run dev:api          # терминал 1; на Windows при отсутствии python3: py -m uvicorn app.main:app --app-dir api --host 127.0.0.1 --port 8000
npm run dev              # терминал 2: https://localhost:5173
```

Переменные для `dev:api`: `FINLENS_QWEN_BASE_URL`, `FINLENS_QWEN_API_KEY`, при необходимости `FINLENS_QWEN_MODEL`, `FINLENS_SESSION_TTL_SECONDS`. `FINLENS_QWEN_PROVIDER=fake` — только для проверки UI-потока, не как production.

## Точная инструкция ручного sideload-теста (Excel Desktop, Windows)

1. Клонировать репозиторий и выполнить команды выше. На `npm run dev:certs` подтвердить установку CA в «Доверенные корневые центры сертификации».
2. Открыть `https://localhost:5173/index.html` в Edge: страница без предупреждения о сертификате. Вне Excel ожидается сообщение «Эта версия Excel не поддерживается».
3. Sideload одним из способов:
   - `npx office-addin-debugging start manifest.xml desktop` (откроет Excel с надстройкой);
   - или общая сетевая папка-каталог: положить `manifest.xml` в общую папку → Excel → Файл → Параметры → Центр управления безопасностью → Параметры → Каталоги надёжных надстроек → добавить URL папки, отметить «Показывать в меню» → перезапустить Excel → Вставка → Мои надстройки → Общая папка → FinLens.
4. Открыть обезличенную книгу (сначала сохранить копию и SHA-256: `certutil -hashfile book.xlsx SHA256`). На вкладке «Главная» нажать «Открыть FinLens».
5. Панель открывается без «не поддерживается»: это подтверждает исправление `Office.onReady`. Плашка показывает «Выделение · Лист · адрес».
6. Проверить сеть: в DevTools панели (F12 через Microsoft Edge DevTools Preview или `office-addin-debugging`) запросы идут на `https://localhost:5173/api/...`, без ошибок CORS/mixed content.
7. Story 1.2/1.3: выделить диапазон, спросить «Что изменилось между годами?». Видны этапы, ответ, источники. Переключить `/sheet` и повторить.
8. Story 1.4: нажать «Книга» и задать вопрос. Видны «Строю карту книги…», «Выбираю релевантные…», «Читаю только выбранные диапазоны (…)», «Готовлю вывод…», затем источники и покрытие листов. Задать уточнение: два хода в истории, карта не перестраивается. Сменить scope: история очищена.
9. С `FINLENS_SESSION_TTL_SECONDS=120` подождать > 2 мин и задать вопрос: сообщение об истёкшем сеансе.
10. Закрыть панель и открыть снова: история пустая. В логах API только IDs, длительности, размеры и коды.
11. Закрыть книгу без сохранения и сравнить SHA-256 с исходным: книга не изменена.
12. Пройти шаги 5–8 только клавиатурой и с экранным диктором: видимый фокус, озвучиваются этапы и ошибки.
13. С production Qwen (не fake) повторить шаги 7–8 и замерить время простого ответа (цель ≤ 1 мин).

## Оставшиеся внешние проверки (обязательны до `done`)

- Ручной sideload-тест в Excel Desktop на Windows (шаги выше).
- Проверка с production Qwen: endpoint и credentials на сервере, качество выбора диапазонов и ответов, latency.
- Независимый code review Stories 1.1–1.4.
- Для деплоя за пределами dev: API и task pane должны остаться на одном HTTPS-origin через корпоративный reverse proxy (решение инфраструктуры; архитектура откладывает network placement).

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
?? src/
?? tsconfig.json
?? vite.config.ts
```
