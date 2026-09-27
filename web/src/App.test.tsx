// Скелет (#297): шапка и счётчики корзин из снимка.
import { screen } from "@testing-library/react";
import { vi } from "vitest";
import App from "./App";
import { emptySnapshot, renderUi } from "./test-utils";

const SNAP = emptySnapshot({
  at: 1, errors: ["nomad: no connection"],
  counts: { puppets: 3, free: 1, busy: 2, sick: 0, silent: 0, down: 0 },
});

vi.mock("./usePool", () => ({ usePool: () => ({ snapshot: SNAP }) }));

test("the header shows the counters and the collector's errors", () => {
  renderUi(<App />);
  expect(screen.getByText("Master Of Puppet")).toBeInTheDocument();
  expect(screen.getByText("3 папета")).toBeInTheDocument();
  expect(screen.getByText("1 свободны")).toBeInTheDocument();
  expect(screen.getByText("2 заняты")).toBeInTheDocument();
  expect(screen.queryByText(/больны/)).toBeNull();
  expect(screen.getByText("nomad: no connection")).toBeInTheDocument();
});

// Подвала нет (просьба оператора 27.09, #301): строка «поток событий · без
// входа… · JSON» убрана со страницы.
test("no footer line", () => {
  renderUi(<App />);
  expect(screen.queryByText(/без входа, LAN доверенная/)).toBeNull();
  expect(screen.queryByText("JSON")).toBeNull();
});
