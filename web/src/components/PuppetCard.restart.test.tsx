// #484: прежние гарантии карточки проверяются через новое место действий,
// строку ростера. Смена цели/роли уничтожает открытое подтверждение.
import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { PoolTable } from "./PoolTable";
import { emptySnapshot, renderUi } from "../test-utils";
import type { Puppet } from "../types";

const ADMIN = { login: "anton", role: "admin" as const };
const CARD: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};
const projects = emptySnapshot({ projects: [{ name: "mop", counts: {
  puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0,
}, puppets: [CARD] }] }).projects;

afterEach(() => vi.unstubAllGlobals());

test("the admin opens a single restart confirm from the row", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PoolTable projects={projects} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "рестарт" }));
  const dialog = await screen.findByRole("dialog");
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(dialog).toHaveTextContent("pu-mop-1");
  expect(dialog).toHaveTextContent("hyper");
  expect(dialog).toHaveTextContent("free (master)");
  expect(vi.mocked(fetch)).not.toHaveBeenCalled();
});

test.each(["target", "role", "login"])("stale restart confirm after %s change rides nowhere", (change) => {
  vi.stubGlobal("fetch", vi.fn());
  const view = renderUi(<PoolTable projects={projects} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "рестарт" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  view.rerender(<MantineProvider><PoolTable projects={change === "target" ? [] : projects}
    session={change === "role" ? { login: null, role: null }
      : change === "login" ? { ...ADMIN, login: "other" } : ADMIN} /></MantineProvider>);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(vi.mocked(fetch)).not.toHaveBeenCalled();
});

test("the row offers update with explicit fresh modes", () => {
  renderUi(<PoolTable projects={projects} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "правка" }));
  const dialog = screen.getByRole("dialog");
  for (const m of ["как было", "свежий клон", "другой origin"]) {
    expect(within(dialog).getByRole("radio", { name: new RegExp(m, "i") })).toBeInTheDocument();
  }
  expect(within(dialog).getAllByRole("radio")
    .filter((r) => (r as HTMLInputElement).checked)).toHaveLength(0);
});

test("a guest never sees restart or update", () => {
  renderUi(<PoolTable projects={projects} session={{ login: null, role: null }} />);
  expect(screen.queryByRole("button", { name: "рестарт" })).toBeNull();
  expect(screen.queryByRole("button", { name: "правка" })).toBeNull();
});
