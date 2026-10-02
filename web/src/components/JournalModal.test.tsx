// Диалог полного события (#422): всё, что сервер отдал в записи, -- на
// столе, длинные поля читаются переносом, фокус trapped, Escape закрывает.
//
// HYPOTHESIS (#422): запись уже безопасна (сервер #424 фильтрует конверт);
// диалог перечисляет поля, какие есть, включая неизвестные заранее
// (контракт расширяется), и ничего не добавляет от себя.
// RESULT: тесты написаны до реализации (RED).
import { fireEvent, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { renderUi } from "../test-utils";
import { JournalModal } from "./JournalModal";
import type { JournalEntry } from "../types";

const E: JournalEntry = {
  at: 1700000000, event: "send", name: "pu-mop-1", node: "hyper", project: "mop",
  text: "from ermak",
};

test("shows every field: full time, event, puppet, node, project, phrase", () => {
  renderUi(<JournalModal entry={E} onClose={() => {}} />);
  const dlg = screen.getByRole("dialog");
  expect(dlg.getAttribute("aria-label") ?? dlg.textContent).toContain("send — pu-mop-1");
  expect(screen.getByText("14.11.2023 22:13:20 UTC")).toBeInTheDocument();
  expect(screen.getByText("hyper")).toBeInTheDocument();
  expect(screen.getByText("mop")).toBeInTheDocument();
  expect(screen.getByText("сообщение от ermak")).toBeInTheDocument();
});

test("extra safe fields of #424 appear as their own rows, not dropped", () => {
  const rich: JournalEntry = { ...E, result: "доставлено", attempts: 3 };
  renderUi(<JournalModal entry={rich} onClose={() => {}} />);
  const dlg = screen.getByRole("dialog");
  // подпись и значение в разных узлах строки -- смотрим текст диалога целиком
  const all = (re: RegExp) => re.test(dlg.textContent ?? "");
  expect(all(/result:\s*доставлено/)).toBe(true);
  expect(all(/attempts:\s*3/)).toBe(true);
});

test("a long text wraps instead of overflowing", () => {
  const long: JournalEntry = { ...E, text: "from " + "ermak-".repeat(60) };
  renderUi(<JournalModal entry={long} onClose={() => {}} />);
  const phrase = screen.getByText(/сообщение от ermak-/);
  expect(phrase.style.whiteSpace).not.toBe("nowrap");
});

test("Escape and the close button call onClose", () => {
  const onClose = vi.fn();
  renderUi(<JournalModal entry={E} onClose={onClose} />);
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
  // крест Mantine -- CloseButton с пустым именем: берём его классом роли
  // (единственная кнопка закрытия в диалоге)
  const closeBtn = screen.getByRole("dialog").querySelector("button.mantine-CloseButton-root");
  expect(closeBtn).not.toBeNull();
  fireEvent.click(closeBtn!);
  expect(onClose).toHaveBeenCalledTimes(2);
});
