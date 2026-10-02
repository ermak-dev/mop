// Страница журнала (#422): полная таблица события шины для админа --
// дата/время целиком, событие, папет, узел, проект, описание; клик по
// строке или клавиатурой открывает доступный диалог с полной записью.
//
// HYPOTHESIS (#422): события приходят из useJournal (GET /api/journal,
// #424), новые сверху; диалог показывает все поля записи, какие сервер
// ни дал, -- без выдумывания своих.
// RESULT: тесты написаны до реализации (RED).
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { renderUi } from "../test-utils";
import { JournalPage } from "./JournalPage";
import type { JournalEntry } from "../types";

const J: JournalEntry[] = [
  { at: 1700000060, event: "idle", name: "pu-mop-1", node: "hyper", project: "mop", text: "" },
  { at: 1700000000, event: "send", name: "pu-mop-1", node: "hyper", project: "mop",
    text: "from ermak", result: "доставлено" },   // безопасное поле #424: показывается, а не теряется
];

test("rows carry full date, event, puppet, node, project and the phrase", () => {
  renderUi(<JournalPage journal={J} open={() => {}} />);
  const row = screen.getByText("14.11.2023 22:14:20").closest("tr")!;
  // дата «полная»: не чч:мм:сс главной, а день и время с секундами
  expect(row.textContent).toContain("idle");
  expect(row.textContent).toContain("pu-mop-1");
  // осмысленное описание: пустой text у idle -- фраза без хвоста
  expect(row.textContent).toContain("дождался простоя");
});

test("clicking a row opens the modal, keyboard Enter too", () => {
  const open = vi.fn();
  renderUi(<JournalPage journal={J} open={open} />);
  // клик по строке: старейшая запись (новые сверху) -- с текстом from ermak
  fireEvent.click(screen.getByText(/сообщение от ermak/));
  expect(open).toHaveBeenCalledWith(J[1]);
  const row = screen.getByText("14.11.2023 22:14:20").closest("tr")!;
  fireEvent.keyDown(row, { key: "Enter" });
  expect(open).toHaveBeenCalledTimes(2);
});

test("keyboard: Enter on the focused row opens the modal too", () => {
  const open = vi.fn();
  renderUi(<JournalPage journal={J} open={open} />);
  const row = screen.getAllByRole("row")[1];   // [0] -- заголовок
  fireEvent.keyDown(row, { key: "Enter" });
  expect(open).toHaveBeenCalledWith(J[1]);
});

test("an empty journal keeps the old note", () => {
  renderUi(<JournalPage journal={[]} open={() => {}} />);
  expect(screen.getByText(/событий пока нет/)).toBeInTheDocument();
});

test("the refusal reason shows instead of a stale journal", () => {
  renderUi(<JournalPage journal={[]} error="журнал недоступен: прав не хватает" open={() => {}} />);
  expect(screen.getByText(/прав не хватает/)).toBeInTheDocument();
  // прежних записей нет: только причина
  expect(screen.queryByRole("row")).toBeNull();
});

// Страница рисует и сам диалог: выбранная запись сразу видна целиком.
test("the page renders the modal for the chosen entry", async () => {
  renderUi(<JournalPage journal={J} open={() => {}} initial={J[1]} />);
  await waitFor(() =>
    expect(screen.getByRole("dialog", { name: /send — pu-mop-1/ })).toBeInTheDocument());
});
