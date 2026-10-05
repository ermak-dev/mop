// #484: прежние гарантии карточки проверяются через новое место действий,
// строку ростера. Смена цели/роли уничтожает открытую операцию.
// #513: рестарт шлёт POST сразу -- проверяется ход операции, не мода.
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
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

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("#513 the restart click posts at once from the row", async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, status: 202,
    json: async () => ({ id: "op-1", status: "accepted" }) }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PoolTable projects={projects} session={ADMIN} />);
  // вход #516: строка -> окно правки -> пункт рестарта
  fireEvent.click(screen.getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "рестарт" }));
  // диалог -- только ход операции, не подтверждение
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops", expect.objectContaining({
    method: "POST", credentials: "same-origin" })));
  expect(screen.queryByText(/подтвердите рестарт/i)).toBeNull();
});

test.each(["target", "role", "login"])("stale restart op after %s change rides nowhere", async (change) => {
  const fetchMock = vi.fn(async () => new Promise<Response>(() => {}));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderUi(<PoolTable projects={projects} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "рестарт" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  view.rerender(<MantineProvider><PoolTable projects={change === "target" ? [] : projects}
    session={change === "role" ? { login: null, role: null }
      : change === "login" ? { ...ADMIN, login: "other" } : ADMIN} /></MantineProvider>);
  // смена источника стирает ход: диалог операции закрыт (#513: моды
  // подтверждения больше нет, stale-проверка смотрит на ход)
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

test("the row offers update with explicit fresh modes", () => {
  renderUi(<PoolTable projects={projects} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "правка" }));
  const win = screen.getByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "правка" }));
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
