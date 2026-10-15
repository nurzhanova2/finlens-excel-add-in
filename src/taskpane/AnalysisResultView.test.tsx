import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnalysisResult, Computation } from "./analysis-api";
import { AnalysisResultView } from "./AnalysisResultView";

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAEklEQVR4nGP4//8/AxJgxOQDAKKXBfvtPbkzAAAAAElFTkSuQmCC";

const computation: Computation = {
  status: "succeeded",
  code: "df = table(\"artifact_1\")\nshow_table(df, \"Рост\")\n",
  method: "Рост рассчитан как отношение 2025 к 2024 минус 1.",
  attempts: 2,
  inputs: [{ sheetName: "P&L", address: "'P&L'!A1:C3" }],
  tables: [{ title: "Рост 2025 к 2024", columns: ["Показатель", "Рост"], rows: [["Выручка", 0.25], ["EBITDA", null]], totalRows: 250, truncated: true }],
  charts: [{ title: "Рост EBITDA", pngBase64: PNG, widthPx: 4, heightPx: 3, description: { metric: "Рост год к году", period: "2024–2025", insight: "Выручка выросла на 25%" } }],
  stdout: "Строк: 2",
  durationMs: 900,
};

const completed: AnalysisResult = {
  analysisId: "analysis_42",
  status: "completed",
  elapsedMs: 1200,
  narrative: { conclusion: "Выручка выросла на 25% (расчёт).", evidence: ["Таблица «Рост 2025 к 2024»"], risks: [], assumptions: [], nextCheck: "Сверьте с отчётом." },
  sources: [{ sheetName: "P&L", address: "'P&L'!A1:C3" }],
  computation,
};

describe("verifiable computation artifacts", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the narrative, sources, analysisId and collapsible Method, Tables, Code and Chart blocks", () => {
    render(<AnalysisResultView result={completed} />);
    const answer = screen.getByRole("article", { name: "Ответ аналитика" });
    expect(answer).toHaveTextContent("Выручка выросла на 25% (расчёт).");
    expect(within(screen.getByRole("list", { name: "Использованные листы и диапазоны" })).getByRole("listitem")).toHaveTextContent(/^P&L · A1:C3$/);
    expect(answer).toHaveTextContent("ID анализа: analysis_42");
    const section = screen.getByRole("region", { name: "Проверяемый расчёт" });
    expect(section).toHaveTextContent("Расчёт выполнен в изолированной песочнице с 2-й попытки (автоматических исправлений: 1 из 2)");

    const summaries = within(section).getAllByText(/^(Методика|Таблицы \(1\)|Код|График \(1\))$/);
    expect(summaries.map((summary) => summary.textContent)).toEqual(["Методика", "Таблицы (1)", "Код", "График (1)"]);
    // Code and tables are collapsed by default; the chart is visible at pane width.
    expect(summaries[1].closest("details")).not.toHaveAttribute("open");
    expect(summaries[2].closest("details")).not.toHaveAttribute("open");
    expect(summaries[3].closest("details")).toHaveAttribute("open");
    expect(within(section).getByText(/Рост рассчитан как отношение/)).toBeInTheDocument();
    expect(within(section).getByText(/Входные данные расчёта: P&L · A1:C3\./)).toBeInTheDocument();
  });

  it("renders result tables with headers, rows and a scrollable keyboard-focusable container", () => {
    render(<AnalysisResultView result={completed} />);
    const table = screen.getByRole("table", { name: "Рост 2025 к 2024" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Показатель", "Рост"]);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(table).toHaveTextContent("0,25");
    expect(table).toHaveTextContent("—");
    const scroll = screen.getByRole("region", { name: "Таблица «Рост 2025 к 2024», прокручивается" });
    expect(scroll).toHaveAttribute("tabindex", "0");
    expect(screen.getByText("Показаны первые 2 из 250 строк результата.")).toBeInTheDocument();
  });

  it("copies exactly the executed code", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<AnalysisResultView result={completed} />);
    expect(document.querySelector("pre.code-block code")?.textContent).toBe(computation.code);
    fireEvent.click(screen.getByRole("button", { name: "Копировать код" }));
    await waitFor(() => expect(screen.getByText("Выполненный код скопирован.")).toBeInTheDocument());
    expect(writeText).toHaveBeenCalledWith(computation.code);
  });

  it("falls back to selecting the code when the clipboard is blocked", async () => {
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    render(<AnalysisResultView result={completed} />);
    fireEvent.click(screen.getByRole("button", { name: "Копировать код" }));
    await waitFor(() => expect(screen.getByText(/текст выделен, нажмите Ctrl\+C/)).toBeInTheDocument());
    expect(window.getSelection()?.toString()).toBe(computation.code);
  });

  it("shows the chart at pane width with a text description and opens a zoom dialog closed by Esc", async () => {
    render(<AnalysisResultView result={completed} />);
    const image = screen.getByRole("img", { name: /График «Рост EBITDA». Метрика: Рост год к году. Период: 2024–2025. Ключевой вывод: Выручка выросла на 25%/ });
    expect(image).toHaveAttribute("src", `data:image/png;base64,${PNG}`);
    const figure = image.closest("figure")!;
    expect(figure).toHaveTextContent("МетрикаРост год к году");
    expect(figure).toHaveTextContent("Период2024–2025");
    expect(figure).toHaveTextContent("Ключевой выводВыручка выросла на 25%");

    const open = screen.getByRole("button", { name: "Увеличить график «Рост EBITDA»" });
    open.focus();
    fireEvent.click(open);
    const dialog = screen.getByRole("dialog", { name: "Рост EBITDA" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const close = within(dialog).getByRole("button", { name: "Закрыть просмотр (Esc)" });
    await waitFor(() => expect(close).toHaveFocus());
    fireEvent.keyDown(document, { key: "Tab" });
    expect(close).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(open).toHaveFocus();

    fireEvent.click(open);
    fireEvent.click(screen.getByRole("button", { name: "Закрыть просмотр (Esc)" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders no code, tables or chart without a successful execution artifact", () => {
    const { rerender } = render(<AnalysisResultView result={{ ...completed, computation: null }} />);
    expect(screen.queryByRole("region", { name: "Проверяемый расчёт" })).not.toBeInTheDocument();
    expect(screen.queryByText("Код")).not.toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();

    // Failures and cancellations are outcomes shown by the pane, never rendered as an answer message.
    rerender(<AnalysisResultView result={{ analysisId: "analysis_42", status: "failed", elapsedMs: 5, computationAttempts: 3, error: { code: "COMPUTATION_FAILED", message: "Расчёт не удалось выполнить после 3 попыток.", nextAction: "Сузьте диапазон." } }} />);
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    rerender(<AnalysisResultView result={{ analysisId: "analysis_42", status: "cancelled", elapsedMs: 5, message: "Анализ отменён.", nextAction: "Задайте новый вопрос." }} />);
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Копировать код" })).not.toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("copies the exact sources as `лист · диапазон` lines", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<AnalysisResultView result={{ ...completed, sources: [{ sheetName: "P&L", address: "'P&L'!A1:C3" }, { sheetName: "Debt", address: "B2:C4" }] }} />);
    fireEvent.click(screen.getByRole("button", { name: "Копировать источники" }));
    await waitFor(() => expect(screen.getByText("Источники скопированы.")).toBeInTheDocument());
    expect(writeText).toHaveBeenCalledWith("P&L · A1:C3\nDebt · B2:C4");
    expect(screen.getByText(/Переход к диапазону из панели не поддерживается/)).toBeInTheDocument();
  });

  it("selects the sources for Ctrl+C when the clipboard is blocked", async () => {
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    render(<AnalysisResultView result={completed} />);
    fireEvent.click(screen.getByRole("button", { name: "Копировать источники" }));
    await waitFor(() => expect(window.getSelection()?.toString()).toContain("P&L · A1:C3"));
  });

  it("says explicitly when only part of the workbook was analysed", () => {
    const { rerender } = render(<AnalysisResultView result={{ ...completed, computation: null, coverage: { sheetsInWorkbook: 12, sheetsWithData: 9, sheetsUsed: ["P&L"], complete: false } }} />);
    expect(screen.getByText(/Проанализирована только часть книги\./).parentElement).toHaveTextContent("Использованы 1 из 9 лист(ов) с данными (всего листов в книге: 12) — P&L, и только диапазоны, перечисленные ниже. Это не анализ всей книги.");
    rerender(<AnalysisResultView result={{ ...completed, computation: null, coverage: { sheetsInWorkbook: 2, sheetsWithData: 1, sheetsUsed: ["P&L"], complete: true } }} />);
    expect(screen.getByText(/прочитаны все листы с данными целиком/)).toBeInTheDocument();
  });

  it("flags insufficient data instead of a confident conclusion", () => {
    render(<AnalysisResultView result={{ ...completed, computation: null, narrative: { ...completed.narrative!, insufficientData: true, missingData: ["Данные за 2024 год"] } }} />);
    const limitation = screen.getByRole("region", { name: "Ограничение вывода" });
    expect(limitation).toHaveTextContent("Данных недостаточно для обоснованного вывода.");
    expect(limitation).toHaveTextContent("Данные за 2024 год");
    expect(screen.getByRole("heading", { name: "Вывод (не подтверждён)" })).toBeInTheDocument();
  });

  it("offers no action that writes to Excel", () => {
    render(<AnalysisResultView result={completed} />);
    const labels = screen.getAllByRole("button").map((button) => button.getAttribute("aria-label") ?? button.textContent);
    expect(labels).toEqual(["Копировать источники", "Копировать код", "Увеличить график «Рост EBITDA»"]);
    expect(document.body.textContent).not.toMatch(/Вставить|в Excel|на лист|Экспорт/);
  });
});
