// Расход по дням (#299): подпись с суммой за окно, столбик на каждый день
// снимка, пустой снимок -- подсказка вместо графика.
import { screen } from "@testing-library/react";
import { UsageChart, usageNote } from "./UsageChart";
import type { UsageDay } from "../types";
import { renderUi } from "../test-utils";

// recharts измеряет контейнер ResizeObserver'ом, которого у jsdom нет:
// заглушку ставит setupTests, и отдельный запуск файла его тоже грузит.

const day = (date: string, total: number): UsageDay =>
  ({ date, total, input: total, output: 0, cache_write: 0, cache_read: 0 });

const DAYS = [day("2026-09-25", 171_000_000), day("2026-09-26", 630_000_000), day("2026-09-27", 35_000_000)];

test("the note carries the window and the grand total", () => {
  expect(usageNote(DAYS)).toBe("за 3 дня, всего 836M");
  expect(usageNote([])).toBe("ещё не собрано");
});

test("the chart renders the title with the note and a bar per day", () => {
  const { container } = renderUi(<UsageChart days={DAYS} />);
  expect(screen.getByText("Токены по дням")).toBeInTheDocument();
  expect(screen.getByText("за 3 дня, всего 836M")).toBeInTheDocument();
  expect(container.querySelector(".mantine-BarChart-root")).not.toBeNull();
});

test("no data: a hint instead of a chart", () => {
  const { container } = renderUi(<UsageChart days={[]} />);
  expect(screen.getByText("расход появится после первого разбора транскриптов")).toBeInTheDocument();
  expect(container.querySelector(".mantine-BarChart-root")).toBeNull();
});
