import { ResultChart, Source } from "./analysis-api";

/** Exact, copyable `лист · диапазон` text; the sheet prefix Excel adds to the address is not repeated. */
export function formatSource(source: Source): string {
  const bang = source.address.lastIndexOf("!");
  return `${source.sheetName} · ${bang >= 0 ? source.address.slice(bang + 1) : source.address}`;
}

export function chartAlt(chart: ResultChart): string {
  const { metric, period, insight } = chart.description;
  return `График «${chart.title}». Метрика: ${metric}. Период: ${period}. Ключевой вывод: ${insight}.`;
}
