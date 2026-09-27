// Узлы (#298): колонки, счётчик в заголовке, корзина по состоянию.
import { screen } from "@testing-library/react";
import { expectHead, renderUi } from "../test-utils";
import { COLS, NodesPanel } from "./NodesPanel";
import type { Node } from "../types";

const NODES: Node[] = [
  { name: "hyper", driver: "pve", serves: "mop,rugent", state: "ready", kind: "free", free_mb: 0, total_mb: 40960, slots: 0, slots_total: 5 },
  { name: "wate-wsl", driver: "host", serves: "any", state: "down", kind: "down", free_mb: null, total_mb: null, slots: null, slots_total: null },
];

test("the nodes table with the old page's columns", () => {
  renderUi(<NodesPanel nodes={NODES} />);
  expect(screen.getByRole("heading", { name: /Узлы/ })).toHaveTextContent("2 в кластере");
  expectHead(Object.values(COLS));
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows[0]).toHaveAttribute("data-kind", "free");
  expect(rows[0]).toHaveTextContent("0 GB");
  expect(rows[0]).toHaveTextContent("40 GB");
  expect(rows[0]).toHaveTextContent("0/5");
  expect(rows[1]).toHaveAttribute("data-kind", "down");
  expect(rows[1]).toHaveTextContent("-");
});

// #326: корзина узла -- поле kind снимка (#325), строку state страница не
// разбирает; снимок без kind -- строка без корзины, а не выдуманная.
test("#326 the row's bucket is the node's kind from the snapshot", () => {
  renderUi(<NodesPanel nodes={[
    { ...NODES[0], name: "a", state: "ready, draining", kind: "busy" },
    { ...NODES[0], name: "b", state: "как угодно", kind: "free" },
    { ...NODES[1], name: "c", state: "ready", kind: undefined },
  ]} />);
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows[0]).toHaveAttribute("data-kind", "busy");
  expect(rows[1]).toHaveAttribute("data-kind", "free");
  expect(rows[2]).not.toHaveAttribute("data-kind");
});

test("no nodes: empty note and no counter", () => {
  renderUi(<NodesPanel nodes={[]} />);
  expect(screen.getByText("пусто")).toBeInTheDocument();
  expect(screen.queryByText(/в кластере/)).toBeNull();
});
