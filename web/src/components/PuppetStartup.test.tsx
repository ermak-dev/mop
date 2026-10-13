// HYPOTHESIS: отсутствие tmux показывается как отказ панели; режим старта
// не передаётся и composer позволяет писать ещё не запущенной сессии.
// SOLUTION: один опрос переключает инициализацию на подтверждённый экран;
// ввод скрыт до него, безопасная причина отказа остаётся конкретной.
// RESULT: ожидание, переход, неизвестный источник и первый сбой были RED.
// STATUS: FIXED — see #612
import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PuppetCard } from "./PuppetCard";
import { renderUi } from "../test-utils";

const ADMIN = { login: "admin", role: "admin" as const };
const response = (source: string, lines: string[], note: string | null = null) => ({
  ok: true, status: 200, json: async () => ({ source, lines, note, truncated: false }),
}) as Response;
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const open = () => renderUi(<PuppetCard session={ADMIN} target="pu-mop-1" onClose={() => {}} />);

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test("#612 startup logs replace the unavailable-panel error and polling enters the live screen", async () => {
  const mock = vi.fn().mockResolvedValueOnce(response("initialization", ["body ready", "TASK [Configure database]"]))
    .mockResolvedValue(response("screen", ["Claude ready"]));
  vi.stubGlobal("fetch", mock);
  open(); await tick();
  expect(screen.getByTestId("tail-lines")).toHaveTextContent("TASK [Configure database]");
  expect(screen.getByText("инициализация")).toBeInTheDocument();
  expect(screen.queryByText(/панель не отвечает/)).toBeNull();
  expect(screen.queryByRole("textbox", { name: "Сообщение" })).toBeNull();
  await tick(3000);
  expect(screen.getByTestId("tail-lines")).toHaveTextContent("Claude ready");
  expect(screen.queryByText("инициализация")).toBeNull();
  expect(screen.getByRole("textbox", { name: "Сообщение" })).toBeInTheDocument();
  expect(mock.mock.calls.every(([url]) => url === "/api/puppet/pu-mop-1/tail")).toBe(true);
});

test("#612 placement wait is a truthful status, not invented output or panel refusal", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response("initialization", [], "Waiting for placement")));
  open(); await tick();
  expect(screen.getByText("Waiting for placement")).toBeInTheDocument();
  expect(screen.getByText("инициализация")).toBeInTheDocument();
  expect(screen.queryByText("вывод пуст")).toBeNull();
  expect(screen.getByTestId("tail-lines")).toHaveTextContent("");
  expect(screen.queryByRole("textbox", { name: "Сообщение" })).toBeNull();
});

test("#612 input is not offered until a confirmed screen source has arrived", async () => {
  let release!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { release = resolve; })));
  open(); await tick();
  expect(screen.queryByRole("textbox", { name: "Сообщение" })).toBeNull();
  await act(async () => { release(response("screen", ["ready"])); });
  expect(screen.getByRole("textbox", { name: "Сообщение" })).toBeInTheDocument();
});

test("#612 a failed first read is not a confirmed live screen", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502 } as Response)));
  open(); await tick();
  expect(screen.getByRole("alert")).toHaveTextContent("панель не отвечает");
  expect(screen.queryByRole("textbox", { name: "Сообщение" })).toBeNull();
});

test("#612 an unknown source is not silently treated as an interactive screen", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response("future-source", ["stage"])));
  open(); await tick();
  expect(screen.queryByRole("textbox", { name: "Сообщение" })).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent("источник вывода неизвестен");
});

test("#612 a safe initialization reader refusal remains specific", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502,
    json: async () => ({ source: "initialization", error: "Nomad client is unreachable" }),
  }) as Response));
  open(); await tick();
  expect(screen.getByRole("alert")).toHaveTextContent("Nomad client is unreachable");
  expect(screen.queryByText(/панель не отвечает/)).toBeNull();
  expect(screen.queryByRole("textbox", { name: "Сообщение" })).toBeNull();
});

test("#612 a real server failure still reports an error and keeps the last startup frame", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response("initialization", ["TASK [Install]"]))
    .mockResolvedValue({ ok: false, status: 502 } as Response));
  open(); await tick(); await tick(3000);
  expect(screen.getByTestId("tail-lines")).toHaveTextContent("TASK [Install]");
  expect(screen.getByRole("alert")).toHaveTextContent("панель не отвечает");
  expect(screen.queryByRole("textbox", { name: "Сообщение" })).toBeNull();
});
