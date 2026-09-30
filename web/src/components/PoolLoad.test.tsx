// Загрузка пула (#378): бейджи мест и корзин, полоса аллокаций по числам
// сервера (web.load) -- страница только рисует.
import { screen } from "@testing-library/react";
import { renderUi } from "../test-utils";
import { LoadBadges, LoadBar } from "./PoolLoad";
import type { Counts, Load } from "../types";

const LOAD: Load = {
  total: 10, allocated: 7, free_slots: 3,
  segments: [
    { kind: "busy", slots: 4 }, { kind: "free", slots: 1 }, { kind: "sick", slots: 0 },
    { kind: "silent", slots: 1 }, { kind: "other", slots: 1 }, { kind: "vacant", slots: 3 },
  ],
};
const COUNTS: Counts = { puppets: 7, free: 1, busy: 4, sick: 0, silent: 1, down: 1 };

test("#378 the badges: slots from the load, buckets from the counts", () => {
  renderUi(<LoadBadges load={LOAD} counts={COUNTS} />);
  const badges = screen.getByTestId("chips");
  for (const text of ["7 папетов", "всего 10 мест", "выделено 7", "4 заняты", "1 свободны",
                      "1 агент молчит", "1 не подняты", "3 свободных места"]) {
    expect(badges).toHaveTextContent(text);
  }
  expect(badges).not.toHaveTextContent(/больны/);
});

test("#378 the bar: segments in the server's order, widths from its numbers", () => {
  renderUi(<LoadBar load={LOAD} />);
  const sections = screen.getAllByTestId(/^load-(?!bar)/);
  // пустой сегмент (sick) не рисуется
  expect(sections.map((s) => s.dataset.testid)).toEqual(
    ["load-busy", "load-free", "load-silent", "load-other", "load-vacant"]);
  expect(sections.map((s) => s.style.getPropertyValue("--progress-section-size")))
    .toEqual(["40%", "10%", "10%", "10%", "30%"]);
});

test("#378 an empty pool draws an empty bar and does not break", () => {
  renderUi(<LoadBar load={{ total: 0, allocated: 0, free_slots: 0, segments: [] }} />);
  expect(screen.getByTestId("load-bar")).toBeInTheDocument();
  expect(screen.queryAllByTestId(/^load-(?!bar)/)).toEqual([]);
});
