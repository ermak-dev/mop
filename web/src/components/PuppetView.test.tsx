// HYPOTHESIS: формы внутри карточки мешают просмотру, tail не показывает
// загрузку, а смена вкладки запрашивает прежний источник.
// SOLUTION: иконки строки открывают отдельные диалоги; просмотр шире,
// запрос связан с выбранной вкладкой, pending виден до конца чтения.
// RESULT: проверены отдельные диалоги, смена источника, спиннер и масштаб.
// STATUS: FIXED — see #484
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PoolTable } from "./PoolTable";
import { PuppetCard } from "./PuppetCard";
import { emptySnapshot, renderUi } from "../test-utils";
import type { Puppet } from "../types";

const session = { login: "admin", role: "admin" as const };
const row: Puppet = {
  name: "pu-mop-1", node: "node", alloc_status: "running", state: "free (master)",
  kind: "free", bucket: "free", detail: null, branch: "master", owner: "",
  llm: "model", origin: "git@example.test:g/mop.git", disk_kb: null,
};
const snapshot = emptySnapshot({ projects: [{ name: "mop", puppets: [row],
  counts: { puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0 } }] });
const response = (body: unknown) => ({ ok: true, json: async () => body }) as Response;
const card = () => renderUi(<PuppetCard snapshot={snapshot} session={session}
  target={row.name} onClose={() => {}} />);

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test.each([
  ["рестарт", "подтвердите рестарт"],
  ["правка", "правка папета"],
  ["модель", "смена модели"],
  ["сообщение", "сообщение папету"],
])("row icon %s opens only its own dialog", async (label, title) => {
  vi.stubGlobal("fetch", vi.fn(async () => response({ models: [] })));
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  const button = within(actions).getByRole("button", { name: label });
  expect(button.querySelector("svg")).not.toBeNull();
  expect(button.textContent).toBe("");
  fireEvent.click(button);
  const dialog = await screen.findByRole("dialog", { name: title });
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(dialog).toHaveTextContent(row.name);
  expect(screen.queryByTestId("puppet-card")).toBeNull();
});

test("read-only viewers have no action icons", () => {
  renderUi(<PoolTable projects={snapshot.projects} session={{ login: "user", role: "user" }} />);
  expect(screen.queryByRole("group", { name: /действия/ })).toBeNull();
});

test("card is a wide viewer, not a collection of action forms", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response({ puppet: row })));
  card();
  await screen.findByText("свободен");
  expect(screen.queryByRole("button", { name: "рестарт" })).toBeNull();
  expect(screen.queryByRole("button", { name: "правка" })).toBeNull();
  expect(screen.queryByRole("button", { name: "модель" })).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByTestId("puppet-card")).toHaveStyle({ "--modal-size": "min(1440px, calc(100vw - 32px))" });
});

test("switching to stderr loads stderr immediately and shows a spinner", async () => {
  let release!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = vi.fn((url: string) => url.endsWith("/stderr") ? pending
    : Promise.resolve(response({ puppet: row })));
  vi.stubGlobal("fetch", fetchMock);
  card();
  fireEvent.click(await screen.findByRole("tab", { name: "stderr" }));
  await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/puppet/pu-mop-1/stderr", expect.anything()));
  expect(screen.getByRole("status", { name: "загрузка stderr" })).toBeInTheDocument();
  await act(async () => { release(response({ lines: ["stderr only"] })); });
  expect(await screen.findByText("stderr only")).toBeInTheDocument();
  expect(screen.queryByRole("status")).toBeNull();
});

test("screen loading replaces old output; empty response is explicit", async () => {
  let release!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = vi.fn().mockResolvedValueOnce(response({ puppet: row }))
    .mockResolvedValueOnce(response({ lines: ["old screen"] })).mockReturnValueOnce(pending);
  vi.stubGlobal("fetch", fetchMock);
  card();
  fireEvent.click(await screen.findByRole("tab", { name: "экран" }));
  await screen.findByText("old screen");
  fireEvent.click(screen.getByRole("button", { name: "обновить" }));
  expect(screen.getByRole("status", { name: "загрузка экрана" })).toBeInTheDocument();
  expect(screen.queryByText("old screen")).toBeNull();
  await act(async () => { release(response({ lines: [] })); });
  expect(await screen.findByText("вывод пуст")).toBeInTheDocument();
  expect(screen.queryByRole("status")).toBeNull();
});

test("terminal output keeps columns and scales down to available width", async () => {
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(1200);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(400);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => response(url.endsWith("/tail")
    ? { lines: ["x".repeat(160), "", "last line"] } : { puppet: row })));
  card();
  fireEvent.click(await screen.findByRole("tab", { name: "экран" }));
  const terminal = await screen.findByTestId("tail-lines");
  expect(terminal).toHaveStyle({ whiteSpace: "pre", transform: "scale(0.5)" });
  expect(terminal.parentElement).toHaveStyle({ overflowY: "hidden", height: "200px" });
  expect(terminal.textContent).toBe(`${"x".repeat(160)}\n\nlast line`);
  fireEvent.click(screen.getByRole("button", { name: "масштаб 1:1" }));
  expect(terminal).toHaveStyle({ transform: "scale(1)" });
  expect(terminal.parentElement).toHaveStyle({ overflowX: "auto", height: "auto" });
  fireEvent.click(screen.getByRole("button", { name: "подогнать по ширине" }));
  expect(terminal).toHaveStyle({ transform: "scale(0.5)" });
});
