// Расход по дням (#299): подпись с суммой за окно, столбик на каждый день
// снимка, пустой снимок -- подсказка вместо графика.
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { UsageChart, usageNote } from "./UsageChart";
import { human, pluralDays } from "./usage-format";
import type { UsageDay } from "../types";

// recharts измеряет контейнер ResizeObserver'ом, которого у jsdom нет;
// setupTests подставляет заглушку, здесь -- своя на случай, если тест
// запускают отдельно.
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
  class { observe() {} unobserve() {} disconnect() {} };

const day = (date: string, total: number): UsageDay =>
  ({ date, total, input: total, output: 0, cache_write: 0, cache_read: 0 });

const DAYS = [day("2026-09-25", 171_000_000), day("2026-09-26", 630_000_000), day("2026-09-27", 35_000_000)];

test("human numbers read like the old page", () => {
  expect(human(171_000_000)).toBe("171M");
  expect(human(2_900)).toBe("2.9k");
  expect(human(836_000_000)).toBe("836M");
  expect(human(1_200_000_000)).toBe("1.2G");
  expect(human(0)).toBe("0");
});

test("days decline in Russian", () => {
  expect(pluralDays(1)).toBe("1 день");
  expect(pluralDays(3)).toBe("3 дня");
  expect(pluralDays(14)).toBe("14 дней");
  expect(pluralDays(21)).toBe("21 день");
});

test("the note carries the window and the grand total", () => {
  expect(usageNote(DAYS)).toBe("за 3 дня, всего 836M");
  expect(usageNote([])).toBe("ещё не собрано");
});

test("the chart renders the title with the note and a bar per day", () => {
  const { container } = render(<MantineProvider><UsageChart days={DAYS} /></MantineProvider>);
  expect(screen.getByText("Токены по дням")).toBeInTheDocument();
  expect(screen.getByText("за 3 дня, всего 836M")).toBeInTheDocument();
  expect(container.querySelector(".mantine-BarChart-root")).not.toBeNull();
});

test("no data: a hint instead of a chart", () => {
  const { container } = render(<MantineProvider><UsageChart days={[]} /></MantineProvider>);
  expect(screen.getByText("расход появится после первого разбора транскриптов")).toBeInTheDocument();
  expect(container.querySelector(".mantine-BarChart-root")).toBeNull();
});
