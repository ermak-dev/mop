// HYPOTHESIS: колонка растягивается и выдаёт результаты в строку; кнопки
// просмотра отсутствуют, масштаб короткого вывода не меняется, скролл общий.
// SOLUTION: компактные правые иконки, отдельные auto-load просмотрщики,
// неподвижная панель и собственная область прокрутки с реальным zoom.
// RESULT: проверены resize по высоте и результат, пришедший в закрытый диалог.
// STATUS: FIXED — see #487
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
const card = (which: "tail" | "stderr" = "tail") => renderUi(<PuppetCard session={session}
  target={row.name} which={which} onClose={() => {}} />);

const originalScrollTop = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTop");
afterEach(() => {
  vi.unstubAllGlobals(); vi.restoreAllMocks();
  if (originalScrollTop) Object.defineProperty(HTMLElement.prototype, "scrollTop", originalScrollTop);
  else Reflect.deleteProperty(HTMLElement.prototype, "scrollTop");
});

test.each([
  ["рестарт", "подтвердите рестарт"], ["правка", "правка папета"],
  ["модель", "смена модели"], ["сообщение", "сообщение папету"],
  ["экран", "экран pu-mop-1"], ["лог", "лог pu-mop-1"],
])("row icon %s opens only its own dialog", async (label, title) => {
  vi.stubGlobal("fetch", vi.fn(async () => response({ models: [], lines: [] })));
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  const button = within(actions).getByRole("button", { name: label });
  expect(button.querySelector("svg")).not.toBeNull();
  expect(button.textContent).toBe("");
  fireEvent.click(button);
  const dialog = await screen.findByRole("dialog", { name: title });
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(dialog).toHaveTextContent(row.name);
});

test("read-only viewers have no action icons", () => {
  renderUi(<PoolTable projects={snapshot.projects} session={{ login: "user", role: "user" }} />);
  expect(screen.queryByRole("group", { name: /действия/ })).toBeNull();
});

test("#487 actions have a right-aligned shrink-to-fit column", () => {
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  expect(screen.getByRole("columnheader", { name: "действия" })).toHaveStyle({ textAlign: "right", width: "1%" });
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  expect(actions).toHaveStyle({ justifyContent: "flex-end", flexWrap: "nowrap" });
  expect(actions.closest("td")).toHaveStyle({ width: "1%", whiteSpace: "nowrap", textAlign: "right" });
  expect(within(actions).getAllByRole("button")).toHaveLength(6);
});

test.each(["рестарт", "правка", "модель"])("#487 %s result never widens the row", async (action) => {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => response(
    init?.method === "POST" ? { id: "op-1", status: "accepted" }
      : url.endsWith("/models") ? { models: ["other-model"] }
      : { id: "op-1", status: "succeeded" })));
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  fireEvent.click(within(actions).getByRole("button", { name: action }));
  if (action === "правка") {
    fireEvent.click(screen.getByRole("radio", { name: "продолжить как было" }));
    fireEvent.click(screen.getByRole("button", { name: "продолжить" }));
  } else if (action === "модель") {
    fireEvent.click(await screen.findByRole("radio", { name: "other-model" }));
    fireEvent.click(screen.getByRole("button", { name: "продолжить" }));
  }
  fireEvent.click(screen.getByRole("button", { name: "подтвердить" }));
  // #487: вердикт строку не расширяет. #488: у модели вердикт уехал в
  // балун (слово оператора); рестарт и правка показывают его модальным
  // диалогом. Инвариант один -- строки действий чисты.
  if (action === "модель") {
    await waitFor(() => expect(actions).not.toHaveTextContent("перезапущен"));
  } else {
    const result = await screen.findByText("перезапущен");
    expect(result.closest('[role="dialog"]')).not.toBeNull();
    expect(actions).not.toHaveTextContent("перезапущен");
  }
});

test.each(["tail", "stderr"] as const)("#487 %s shows loading on open", async (which) => {
  let release!: (r: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { release = resolve; })));
  card(which);
  expect(screen.getByRole("status")).toBeInTheDocument();
  expect(vi.mocked(fetch)).toHaveBeenCalledWith(`/api/puppet/pu-mop-1/${which}`, expect.anything());
  await act(async () => { release(response({ lines: ["loaded"] })); });
  expect(await screen.findByText("loaded")).toBeInTheDocument();
  expect(screen.queryByRole("status")).toBeNull();
});

test("refresh replaces old output with loading and names an empty result", async () => {
  let release!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { release = resolve; });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response({ lines: ["old screen"] })).mockReturnValueOnce(pending));
  card();
  await screen.findByText("old screen");
  fireEvent.click(screen.getByRole("button", { name: "обновить" }));
  expect(screen.getByRole("status")).toBeInTheDocument();
  expect(screen.queryByText("old screen")).toBeNull();
  await act(async () => { release(response({ lines: [] })); });
  expect(await screen.findByText("вывод пуст")).toBeInTheDocument();
});

test.each([300, 1200])("#487 zoom changes even when %s-pixel text already fits", async (natural) => {
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(natural);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(400);
  vi.stubGlobal("fetch", vi.fn(async () => response({ lines: ["first", "", "last"] })));
  card();
  const terminal = await screen.findByTestId("tail-lines");
  const initial = Math.min(1, 600 / natural);
  expect(terminal).toHaveStyle({ whiteSpace: "pre", transform: `scale(${initial})` });
  expect(terminal.textContent).toBe("first\n\nlast");
  fireEvent.click(screen.getByRole("button", { name: "увеличить" }));
  expect(terminal).toHaveStyle({ transform: `scale(${initial + 0.25})` });
  fireEvent.click(screen.getByRole("button", { name: "уменьшить" }));
  expect(terminal).toHaveStyle({ transform: `scale(${initial})` });
  fireEvent.click(screen.getByRole("button", { name: "по ширине" }));
  expect(terminal).toHaveStyle({ transform: `scale(${initial})` });
  expect(screen.getByRole("toolbar", { name: "просмотр" }).closest('[data-testid="output-scroll"]')).toBeNull();
  expect(screen.getByTestId("output-scroll")).toHaveStyle({ overflow: "auto", minHeight: "0" });
});

test("#487 height-only resize keeps the last log line visible", async () => {
  let height = 300;
  let top = 0;
  let notify = () => {};
  vi.stubGlobal("ResizeObserver", class {
    constructor(private callback: () => void) {}
    observe(el: HTMLElement) { if (el.dataset.testid === "output-scroll") notify = this.callback; }
    unobserve() {}
    disconnect() {}
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => height);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(300);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(1200);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(1200);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "get").mockImplementation(() => top);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "set").mockImplementation((value) => { top = Math.min(value, 1200 - height); });
  vi.stubGlobal("fetch", vi.fn(async () => response({ lines: ["last log line"] })));
  card("stderr");
  await screen.findByText("last log line");
  expect(top).toBe(900);
  await act(async () => { height = 100; notify(); });
  expect(top).toBe(1100);
});

test.each(["рестарт", "правка", "модель"])("#487 %s preserves a result completed while its dialog was closed", async (action) => {
  let finish!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  const mock = vi.fn(async (url: string, init?: RequestInit) => init?.method === "POST"
    ? response({ id: "op-1", status: "accepted" })
    : url.endsWith("/models") ? response({ models: ["other-model"] }) : pending);
  vi.stubGlobal("fetch", mock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  fireEvent.click(within(actions).getByRole("button", { name: action }));
  if (action === "правка") {
    fireEvent.click(screen.getByRole("radio", { name: "продолжить как было" }));
    fireEvent.click(screen.getByRole("button", { name: "продолжить" }));
  } else if (action === "модель") {
    fireEvent.click(await screen.findByRole("radio", { name: "other-model" }));
    fireEvent.click(screen.getByRole("button", { name: "продолжить" }));
  }
  fireEvent.click(screen.getByRole("button", { name: "подтвердить" }));
  if (action === "модель") {
    // #488: вердикт модели -- балун; ход живёт в useAdminOp, повторная
    // команда открывает форму, дедуп на сервере (id на месте).
    await waitFor(() => expect(mock.mock.calls
      .filter((c) => c[1]?.method === "POST")).toHaveLength(1));
    fireEvent.click(within(actions).getByRole("button", { name: action }));
    expect(await screen.findByTestId("model-choices")).toBeInTheDocument();
  } else {
    await screen.findByText("принято");
    fireEvent.click(screen.getByRole("button", { name: "закрыть" }));
    await act(async () => { finish(response({ status: "failed", error: "review refusal" })); });
    fireEvent.click(within(actions).getByRole("button", { name: action }));
    expect(await screen.findByText(/review refusal/)).toBeInTheDocument();
    expect(mock.mock.calls.filter((c) => c[1]?.method === "POST")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "новая команда" }));
    expect(screen.queryByText(/review refusal/)).toBeNull();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(mock.mock.calls.filter((c) => c[1]?.method === "POST")).toHaveLength(1);
  }
});

test("#487 opening a log scrolls its viewport to the end", async () => {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(1200);
  vi.stubGlobal("fetch", vi.fn(async () => response({ lines: ["oldest", "latest"] })));
  card("stderr");
  await screen.findByText("latest");
  expect(screen.getByTestId("output-scroll").scrollTop).toBe(1200);
});
