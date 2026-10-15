import { useEffect, useId, useRef, useState } from "react";
import { AnalysisResult, Computation, Coverage, MAX_EXECUTION_ATTEMPTS, ResultChart, ResultTable, Source, TableCell } from "./analysis-api";
import { CopyButton } from "./copy";
import { chartAlt, formatSource } from "./format";

// Read-only rendering: results stay in the task pane; nothing here touches Office.js or the workbook.

function formatCell(value: TableCell): string {
  if (value === null) return "—";
  if (typeof value === "number") return value.toLocaleString("ru-RU", { maximumFractionDigits: 10 });
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  return value;
}

function ChartDescriptionText({ chart }: { chart: ResultChart }) {
  return (
    <dl className="chart-description">
      <dt>Метрика</dt><dd>{chart.description.metric}</dd>
      <dt>Период</dt><dd>{chart.description.period}</dd>
      <dt>Ключевой вывод</dt><dd>{chart.description.insight}</dd>
    </dl>
  );
}

function ChartViewer({ chart, onClose }: { chart: ResultChart; onClose: () => void }) {
  const titleId = useId();
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "Tab") {
        // The close button is the only control: keep focus inside the dialog.
        event.preventDefault();
        closeButton.current?.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="chart-viewer" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="chart-viewer__dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId}>{chart.title}</h2>
        <img src={`data:image/png;base64,${chart.pngBase64}`} alt={chartAlt(chart)} width={chart.widthPx} height={chart.heightPx} />
        <ChartDescriptionText chart={chart} />
        <button type="button" ref={closeButton} className="secondary-button" onClick={onClose}>
          Закрыть просмотр (Esc)
        </button>
      </div>
    </div>
  );
}

function TableBlock({ table }: { table: ResultTable }) {
  return (
    <div className="result-table">
      <div className="result-table__scroll" role="region" aria-label={`Таблица «${table.title}», прокручивается`} tabIndex={0}>
        <table>
          <caption>{table.title}</caption>
          <thead>
            <tr>{table.columns.map((column, index) => <th key={`${index}-${column}`} scope="col">{column}</th>)}</tr>
          </thead>
          <tbody>
            {table.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{formatCell(cell)}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
      {table.truncated && <p className="help-text">Показаны первые {table.rows.length} из {table.totalRows} строк результата.</p>}
    </div>
  );
}

function CodeBlock({ code }: { code: string }) {
  const codeElement = useRef<HTMLElement>(null);
  return (
    <>
      <p className="help-text">Показан только код, который фактически выполнен в песочнице и дал этот результат.</p>
      <CopyButton text={code} target={codeElement} label="Копировать код" done="Выполненный код скопирован." />
      <pre className="code-block"><code ref={codeElement}>{code}</code></pre>
    </>
  );
}

function CoverageNote({ coverage }: { coverage: Coverage }) {
  const used = `${coverage.sheetsUsed.length} из ${coverage.sheetsWithData} лист(ов) с данными (всего листов в книге: ${coverage.sheetsInWorkbook}) — ${coverage.sheetsUsed.join(", ")}`;
  return coverage.complete ? (
    <p className="analysis-result__coverage"><strong>Покрытие книги:</strong> прочитаны все листы с данными целиком: {used}.</p>
  ) : (
    <p className="analysis-result__coverage analysis-result__coverage--partial">
      <strong>Проанализирована только часть книги.</strong> Использованы {used}, и только диапазоны, перечисленные ниже. Это не анализ всей книги.
    </p>
  );
}

function SourceList({ sources }: { sources: Source[] }) {
  const list = useRef<HTMLUListElement>(null);
  const lines = sources.map(formatSource);
  return (
    <>
      <ul className="analysis-result__sources" ref={list} aria-label="Использованные листы и диапазоны">
        {lines.map((line) => <li key={line}>{line}</li>)}
      </ul>
      <CopyButton text={lines.join("\n")} target={list} label="Копировать источники" done="Источники скопированы." />
      <p className="help-text">Переход к диапазону из панели не поддерживается: найдите лист и адрес вручную по точному тексту выше.</p>
    </>
  );
}

function ComputationDetails({ computation }: { computation: Computation }) {
  const [zoomed, setZoomed] = useState<number | null>(null);
  const triggers = useRef<(HTMLButtonElement | null)[]>([]);
  const corrections = computation.attempts - 1;

  function closeViewer() {
    const index = zoomed;
    setZoomed(null);
    if (index !== null) triggers.current[index]?.focus();
  }

  return (
    <section className="computation" aria-label="Проверяемый расчёт">
      <p className="computation__badge">
        <strong>Расчёт выполнен</strong> в изолированной песочнице
        {corrections > 0 ? ` с ${computation.attempts}-й попытки (автоматических исправлений: ${corrections} из ${MAX_EXECUTION_ATTEMPTS - 1})` : " с первой попытки"}.
      </p>
      <details>
        <summary>Методика</summary>
        <p>{computation.method}</p>
        <p>Входные данные расчёта: {computation.inputs.map(formatSource).join("; ")}.</p>
        {computation.stdout.trim() && <><p>Вывод программы:</p><pre className="code-block">{computation.stdout}</pre></>}
      </details>
      {computation.tables.length > 0 && (
        <details>
          <summary>Таблицы ({computation.tables.length})</summary>
          {computation.tables.map((table, index) => <TableBlock key={`${index}-${table.title}`} table={table} />)}
        </details>
      )}
      <details>
        <summary>Код</summary>
        <CodeBlock code={computation.code} />
      </details>
      {computation.charts.length > 0 && (
        <details open>
          <summary>График ({computation.charts.length})</summary>
          {computation.charts.map((chart, index) => (
            <figure key={`${index}-${chart.title}`} className="chart">
              <button
                type="button"
                className="chart__open"
                ref={(element) => { triggers.current[index] = element; }}
                onClick={() => setZoomed(index)}
                aria-label={`Увеличить график «${chart.title}»`}
              >
                <img src={`data:image/png;base64,${chart.pngBase64}`} alt={chartAlt(chart)} />
              </button>
              <figcaption>
                <strong>{chart.title}</strong>
                <ChartDescriptionText chart={chart} />
              </figcaption>
            </figure>
          ))}
        </details>
      )}
      {zoomed !== null && computation.charts[zoomed] && <ChartViewer chart={computation.charts[zoomed]} onClose={closeViewer} />}
    </section>
  );
}

/** Only completed answers are rendered as messages; failures and cancellations are outcomes, not answers. */
export function AnalysisResultView({ result }: { result: AnalysisResult }) {
  if (result.status !== "completed" || !result.narrative) return null;
  const { narrative, sources, coverage, computation } = result;
  return (
    <article className="analysis-result" aria-label="Ответ аналитика">
      {narrative.insufficientData && (
        <section className="analysis-result__limitation" aria-label="Ограничение вывода">
          <p><strong>Данных недостаточно для обоснованного вывода.</strong> Вывод ниже предварительный и не подтверждён данными.</p>
          <p>Не хватает:</p>
          <ul>{(narrative.missingData ?? []).map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>
        </section>
      )}
      <h2>{narrative.insufficientData ? "Вывод (не подтверждён)" : "Вывод"}</h2><p>{narrative.conclusion}</p>
      <h3>Доказательства</h3><ul>{narrative.evidence.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>
      {(narrative.risks.length > 0 || narrative.assumptions.length > 0) && <><h3>Риски и допущения</h3><ul>{[...narrative.risks, ...narrative.assumptions].map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></>}
      <h3>Следующая проверка</h3><p>{narrative.nextCheck}</p>
      <h3>Источники</h3>
      {coverage && <CoverageNote coverage={coverage} />}
      <SourceList sources={sources ?? []} />
      <p className="help-text">ID анализа: <span className="analysis-result__id">{result.analysisId}</span></p>
      {computation && <ComputationDetails computation={computation} />}
    </article>
  );
}
