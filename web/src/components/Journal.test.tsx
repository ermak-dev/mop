// Журнал (#298): новые сверху, та же строка, что раньше.
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { EMPTY, Journal } from "./Journal";
import type { JournalEntry } from "../types";

const J: JournalEntry[] = [
  { at: 1700000000, event: "send", name: "pu-mop-1", node: "hyper", project: "mop", text: "from ermak" },
  { at: 1700000060, event: "idle", name: "pu-mop-1", node: "hyper", project: "mop", text: "" },
];

test("entries newest first with event, puppet, node and project", () => {
  render(<MantineProvider><Journal journal={J} /></MantineProvider>);
  const items = screen.getAllByText(/на hyper, проект mop/);
  expect(items).toHaveLength(2);
  const texts = items.map((el) => el.parentElement?.textContent ?? "");
  expect(texts[0]).toContain("idle");
  expect(texts[1]).toContain("send");
  expect(texts[1]).toContain("from ermak");
});

test("no events: the same note as the old page", () => {
  render(<MantineProvider><Journal journal={[]} /></MantineProvider>);
  expect(screen.getByText(EMPTY)).toBeInTheDocument();
});
