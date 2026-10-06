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

test("#513 the restart click posts from the final screen", async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, status: 202,
    json: async () => ({ id: "op-1", status: "accepted" }) }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PoolTable projects={projects} session={ADMIN} />);
  // вход #522: строка -> визард правки -> финальный экран рестарта ->
  // «Завершить» = согласие (единое правило подтверждения)
  fireEvent.click(screen.getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "Рестарт" }));
  // до согласия POST нет: финальный экран сам и есть подтверждение
  expect(win.textContent).toMatch(/Контекст диалога с Claude теряется/);
  expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops", expect.objectContaining({
    method: "POST", credentials: "same-origin" })));
});

test.each(["target", "role", "login"])("stale restart op after %s change rides nowhere", async (change) => {
  const fetchMock = vi.fn(async () => new Promise<Response>(() => {}));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderUi(<PoolTable projects={projects} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "Рестарт" }));
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  view.rerender(<MantineProvider><PoolTable projects={change === "target" ? [] : projects}
    session={change === "role" ? { login: null, role: null }
      : change === "login" ? { ...ADMIN, login: "other" } : ADMIN} /></MantineProvider>);
  // смена источника стирает ход: экран хода исчез вместе с окном цели
  await waitFor(() => expect(screen.queryByTestId("control-window")).toBeNull());
});

test("the row offers update with explicit fresh modes", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PoolTable projects={projects} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "Правка" }));
  const win = screen.getByTestId("control-window");
  // #527: пункт update ведёт на СВОЙ шаг выбора режима -- три радио,
  // не смешанные с кнопками чужих операций
  fireEvent.click(within(win).getByRole("button", { name: "Обновление" }));
  const dialog = screen.getByRole("dialog");
  for (const m of ["Продолжить работу", "Пересобрать чистый клон",
                   "Переключить на другой проект"]) {
    expect(within(dialog).getByRole("radio", { name: new RegExp(m, "i") })).toBeInTheDocument();
  }
  expect(within(dialog).getAllByRole("radio")
    .filter((r) => (r as HTMLInputElement).checked)).toHaveLength(0);
});

test("a guest never sees restart or update", () => {
  renderUi(<PoolTable projects={projects} session={{ login: null, role: null }} />);
  expect(screen.queryByRole("button", { name: "Рестарт" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Правка" })).toBeNull();
});
