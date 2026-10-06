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

const ADMIN = { login: "admin", role: "admin" as const };
const ok = (lines: string[]) => ({ ok: true, status: 200, json: async () => ({ lines }) }) as Response;
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const open = () => renderUi(
  <PuppetCard session={ADMIN} target="pu-mop-1" onClose={() => {}} />);

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test("#489 the composer sits inside the screen dialog", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ok(["screen"])));
  open(); await tick();
  const composer = screen.getByRole("textbox", { name: "Сообщение" });
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
  const input = screen.getByRole("textbox", { name: "Сообщение" });
  fireEvent.change(input, { target: { value: "unsent draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Увеличить" }));
  input.focus();
  const viewport = screen.getByTestId("output-scroll");
  Object.defineProperty(viewport, "scrollHeight", { value: 2000 });
  Object.defineProperty(viewport, "clientHeight", { value: 300 });
  viewport.scrollTop = 125; fireEvent.scroll(viewport);
  await tick(3000);
  expect(mock).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("button", { name: "Обновить" })).toBeEnabled();
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

test.each([401, 403])("#489 HTTP %s removes private output and composer and stops polling", async (status) => {
  const mock = vi.fn().mockResolvedValueOnce(ok(["private output"]))
    .mockResolvedValue({ ok: false, status });
  vi.stubGlobal("fetch", mock);
  open(); await tick();
  await tick(3000);
  expect(screen.queryByText("private output")).toBeNull();
  expect(screen.queryByRole("textbox", { name: "Сообщение" })).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent(/прав не хватает/);
  await tick(9000);
  expect(mock).toHaveBeenCalledTimes(2);
});
