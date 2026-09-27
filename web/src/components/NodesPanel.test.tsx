// Узлы (#298): колонки, счётчик в заголовке, корзина по состоянию.
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { NodesPanel } from "./NodesPanel";
import type { Node } from "../types";

const NODES: Node[] = [
  { name: "hyper", driver: "pve", serves: "mop,rugent", state: "ready", free_mb: 0, total_mb: 40960, slots: 0, slots_total: 5 },
  { name: "wate-wsl", driver: "host", serves: "any", state: "down", free_mb: null, total_mb: null, slots: null, slots_total: null },
];

test("the nodes table with the old page's columns", () => {
  render(<MantineProvider><NodesPanel nodes={NODES} /></MantineProvider>);
  expect(screen.getByRole("heading", { name: /Узлы/ })).toHaveTextContent("2 в кластере");
  for (const h of ["узел", "драйвер", "проекты", "состояние", "свободно", "всего", "слоты"]) {
    expect(screen.getByRole("columnheader", { name: h })).toBeInTheDocument();
  }
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows[0]).toHaveAttribute("data-kind", "free");
  expect(rows[0]).toHaveTextContent("0 GB");
  expect(rows[0]).toHaveTextContent("40 GB");
  expect(rows[0]).toHaveTextContent("0/5");
  expect(rows[1]).toHaveAttribute("data-kind", "down");
  expect(rows[1]).toHaveTextContent("-");
});

test("no nodes: empty note and no counter", () => {
  render(<MantineProvider><NodesPanel nodes={[]} /></MantineProvider>);
  expect(screen.getByText("пусто")).toBeInTheDocument();
  expect(screen.queryByText(/в кластере/)).toBeNull();
});
