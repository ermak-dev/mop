// HYPOTHESIS: обновление размонтирует вывод, таймера нет, send живёт отдельно.
// SOLUTION: стабильный вывод и форма; последовательный опрос только экрана,
// временный сбой сохраняет кадр, отказ прав его удаляет и останавливает опрос.
// RESULT: проверены DOM, черновик, фокус, масштаб, прокрутка и остановка.
// STATUS: FIXED — see #489
import { act, fireEvent, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PuppetCard } from "./PuppetCard";
import { renderUi } from "../test-utils";

// #661: snapshot-проверки используют startup; чат и терминал больше не split.
const ADMIN = { login: "admin", role: "admin" as const };
const ok = (lines: string[], source = 'initialization') => ({ ok: true, status: 200, json: async () => ({ source, lines }) }) as Response;
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const open = () => {
  const fetchTail = vi.mocked(fetch);
  vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
    if (String(url).includes('/history')) return Promise.resolve({ ok: true, status: 200, json: async () => ({ epoch: 'e', session_id: 's', cursor: 'c', records: [], reset: false, partial: false, has_more: false }) });
    if (String(url).endsWith('/term')) return Promise.resolve({ ok: true, status: 200, json: async () => ({ tid: 'writer', epoch: 'pty', cursor: 0, mode: 'rw', cols: 80, rows: 24 }) } as Response);
    if (String(url).includes('/read?')) return new Promise<Response>(() => {});
    if (String(url).endsWith('/close')) return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) } as Response);
    return fetchTail(url, init);
  });
  const view = renderUi(<PuppetCard session={ADMIN} target="pu-mop-1" onClose={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  return view;
};

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test("#489 the composer sits inside the screen dialog", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ok(["screen"])));
  open(); await tick();
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  const composer = screen.getByRole("textbox", { name: "Ввод человека в CLI" });
  expect(composer.closest('[data-testid="output-scroll"]')).toBeNull();
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
});

test("#489 polling keeps the same terminal, draft, focus, zoom and scroll", async () => {
  let release!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { release = resolve; });
  const mock = vi.fn().mockResolvedValueOnce(ok(["old screen"])).mockReturnValueOnce(pending);
  vi.stubGlobal("fetch", mock);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(300);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(2000);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600);
  open(); await tick();
  const terminal = screen.getByTestId("tail-lines");
  fireEvent.click(screen.getByRole("button", { name: "Увеличить" }));
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  const input = screen.getByRole("textbox", { name: "Ввод человека в CLI" });
  fireEvent.change(input, { target: { value: "unsent draft" } });
  input.focus();
  const viewport = screen.getByTestId("output-scroll");
  Object.defineProperty(viewport, "scrollHeight", { value: 2000 });
  Object.defineProperty(viewport, "clientHeight", { value: 300 });
  viewport.scrollTop = 125; fireEvent.scroll(viewport);
  await tick(3000);
  expect(mock).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText("Обновить")).toBeEnabled();
  expect(screen.getByTestId("tail-lines")).toBe(terminal);
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByText("old screen")).toBeInTheDocument();
  await act(async () => { release(ok(["new screen"])); });
  expect(screen.getByTestId("tail-lines")).toBe(terminal);
  expect(terminal).toHaveTextContent("new screen");
  expect(terminal).toHaveStyle({ transform: "scale(1.25)" });
  expect(input).toHaveValue("unsent draft");
  expect(document.activeElement).toBe(input);
  expect(viewport.scrollTop).toBe(125);
});

test("#489 slow requests never overlap and the next poll waits for completion", async () => {
  let release!: (r: Response) => void;
  const mock = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve; }))
    .mockResolvedValue(ok(["next"]));
  vi.stubGlobal("fetch", mock);
  open(); await tick(12000);
  expect(mock).toHaveBeenCalledTimes(1);
  await act(async () => { release(ok(["first"])); });
  await tick(2999); expect(mock).toHaveBeenCalledTimes(1);
  await tick(1); expect(mock).toHaveBeenCalledTimes(2);
});

test("#489 manual refresh cannot overlap with the polling timer", async () => {
  let release!: (r: Response) => void;
  const mock = vi.fn().mockResolvedValueOnce(ok(["first"]))
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve; }))
    .mockResolvedValue(ok(["next"]));
  vi.stubGlobal("fetch", mock);
  open(); await tick(2500);
  fireEvent.click(screen.getByRole("button", { name: "Обновить" }));
  await tick(6000);
  expect(mock).toHaveBeenCalledTimes(2);
  await act(async () => { release(ok(["manual"])); });
  await tick(3000);
  expect(mock).toHaveBeenCalledTimes(3);
});

test.each(["close", "role", "target"])("#489 %s stops polling and ignores a late answer", async (change) => {
  let release!: (r: Response) => void;
  const mock = vi.fn().mockResolvedValueOnce(ok(["first"]))
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve; }))
    .mockResolvedValue(ok(["other target"]));
  vi.stubGlobal("fetch", mock);
  const view = open(); await tick(3000);
  expect(mock).toHaveBeenCalledTimes(2);
  view.rerender(<MantineProvider><PuppetCard session={change === "role" ? { login: null, role: null } : ADMIN}
    target={change === "target" ? "pu-mop-2" : change === "close" ? null : "pu-mop-1"}
    onClose={() => {}} /></MantineProvider>);
  await act(async () => { release(ok(["late private output"])); });
  await tick(9000);
  expect(screen.queryByText("late private output")).toBeNull();
  expect(mock.mock.calls.filter((c) => String(c[0]).includes("pu-mop-1"))).toHaveLength(2);
});

// #507: источник один -- экран, и он один решает расписание опроса; ветки
// stderr и её «без авто-обновления» больше нет.
test("#489 the screen keeps polling on its timer", async () => {
  const mock = vi.fn(async () => ok(["log"])); vi.stubGlobal("fetch", mock);
  open(); await tick(12000);
  expect(mock.mock.calls.length).toBeGreaterThanOrEqual(3);
});

test("#489 a transient failure keeps the last frame and recovers without remounting", async () => {
  const mock = vi.fn().mockResolvedValueOnce(ok(["last frame"]))
    .mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(ok(["recovered"]));
  vi.stubGlobal("fetch", mock);
  open(); await tick();
  const terminal = screen.getByTestId("tail-lines");
  await tick(3000);
  expect(screen.getByTestId("tail-lines")).toBe(terminal);
  expect(terminal).toHaveTextContent("last frame");
  expect(screen.getByRole("alert")).toHaveTextContent(/не отвечает/);
  await tick(3000);
  expect(screen.getByTestId("tail-lines")).toBe(terminal);
  expect(terminal).toHaveTextContent("recovered");
  expect(screen.queryByRole("alert")).toBeNull();
});

test.each([401, 403])("#661 HTTP %s clears private output, revokes input and stops polling", async (status) => {
  const mock = vi.fn().mockResolvedValueOnce(ok(["private output"], 'screen'))
    .mockResolvedValue({ ok: false, status });
  vi.stubGlobal("fetch", mock);
  open(); await tick();
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  const input = screen.getByRole('textbox', { name: 'Ввод человека в CLI' });
  fireEvent.change(input, { target: { value: 'private draft' } });
  expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled();
  await tick(3000);
  expect(screen.queryByText("private output")).toBeNull();
  // #661: удалённый channel composer сменился постоянным CLI-полем;
  // потеря прав запрещает отправку, даже если пользователь введёт черновик.
  fireEvent.change(screen.getByRole('textbox', { name: 'Ввод человека в CLI' }), { target: { value: 'must not send' } });
  expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  expect(screen.getAllByRole('alert').some(el => /прав не хватает|авторизация потеряна/.test(el.textContent ?? ''))).toBe(true);
  await tick(9000);
  expect(mock).toHaveBeenCalledTimes(2);
});
