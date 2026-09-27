// Скелет (#297): шапка и счётчики корзин из снимка.
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { vi } from "vitest";
import App, { plural } from "./App";
import type { Snapshot } from "./types";

const SNAP: Snapshot = {
  at: 1, projects: [], nodes: [], usage: [], per_puppet: {}, per_user: [], journal: [], errors: ["nomad: no connection"], creds: [], masters: [],
  counts: { puppets: 3, free: 1, busy: 2, sick: 0, silent: 0, down: 0 },
};

vi.mock("./usePool", () => ({ usePool: () => ({ snapshot: SNAP, live: true }) }));

test("plural", () => {
  expect(plural(1, "папет", "папета", "папетов")).toBe("1 папет");
  expect(plural(3, "папет", "папета", "папетов")).toBe("3 папета");
  expect(plural(11, "папет", "папета", "папетов")).toBe("11 папетов");
  expect(plural(22, "папет", "папета", "папетов")).toBe("22 папета");
});

test("the header shows the counters and the collector's errors", () => {
  render(<MantineProvider><App /></MantineProvider>);
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
  render(<MantineProvider><App /></MantineProvider>);
  expect(screen.queryByText(/без входа, LAN доверенная/)).toBeNull();
  expect(screen.queryByText("JSON")).toBeNull();
});
