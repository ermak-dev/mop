// HYPOTHESIS: отсутствие tmux показывается как отказ панели; режим старта
// не передаётся и composer позволяет писать ещё не запущенной сессии.
// SOLUTION: один опрос переключает инициализацию на подтверждённый экран;
// ввод скрыт до него, безопасная причина отказа остаётся конкретной.
// RESULT: ожидание, переход, неизвестный источник и первый сбой были RED.
// STATUS: FIXED — see #612
import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PuppetCard } from "./PuppetCard";
import { renderUi } from "../test-utils";

vi.mock('@xterm/xterm', () => ({ Terminal: class {
  cols = 80; rows = 24; options = { disableStdin: true };
  parser = { registerOscHandler: () => ({ dispose() {} }) };
  loadAddon() {} open() {} dispose() {} reset() {} write() {}
  onData() { return { dispose() {} }; } onBinary() { return { dispose() {} }; }
  onResize() { return { dispose() {} }; }
} }));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} dispose() {} } }));

const ADMIN = { login: "admin", role: "admin" as const };
const response = (source: string, lines: string[], note: string | null = null) => ({
  ok: true, status: 200, json: async () => ({ source, lines, note, truncated: false }),
}) as Response;
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
// #661: управление берётся один раз автоматически, только после screen.
const termOpen = vi.fn();
const open = () => {
  const fetchTail = vi.mocked(fetch);
  vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
    if (String(url).includes('/history')) return Promise.resolve({ ok: true, status: 200, json: async () => ({ epoch: 'e', session_id: 's', cursor: 'c', records: [], reset: false, partial: false, has_more: false }) });
    if (String(url).includes('/read?')) return new Promise(() => {});
    if (String(url).endsWith('/close')) return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
    if (String(url).endsWith('/term') && init?.method === 'POST') {
      termOpen(url, init);
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ tid: 'rw', epoch: 'pty', cursor: 0, mode: 'rw', cols: 80, rows: 24 }) });
    }
    return fetchTail(url, init);
  });
  const view = renderUi(<PuppetCard session={ADMIN} target="pu-mop-1" onClose={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  return view;
};

beforeEach(() => { vi.useFakeTimers(); termOpen.mockClear(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test("#612 startup logs replace the unavailable-panel error and polling enters the live screen", async () => {
  const mock = vi.fn().mockResolvedValueOnce(response("initialization", ["body ready", "TASK [Configure database]"]))
    .mockResolvedValue(response("screen", ["Claude ready"]));
  vi.stubGlobal("fetch", mock);
  open(); await tick();
  expect(screen.getByTestId("tail-lines")).toHaveTextContent("TASK [Configure database]");
  expect(screen.getByText("инициализация")).toBeInTheDocument();
  expect(screen.queryByText(/панель не отвечает/)).toBeNull();
  expect(termOpen).not.toHaveBeenCalled();
  await tick(3000);
  expect(screen.getByRole('region', { name: 'Терминал: управление' })).toBeVisible();
  expect(screen.getByTestId('tail-lines')).toHaveTextContent('Claude ready');
  expect(screen.getByTestId('tail-lines')).not.toBeVisible();
  expect(termOpen).toHaveBeenCalledTimes(1);
  expect(JSON.parse(String(termOpen.mock.calls[0][1].body)).mode).toBe('rw');
  expect(mock.mock.calls.every(([url]) => url === '/api/puppet/pu-mop-1/tail')).toBe(true);
});

test("#612 placement wait is a truthful status, not invented output or panel refusal", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response("initialization", [], "Waiting for placement")));
  open(); await tick();
  expect(screen.getByText("Waiting for placement")).toBeInTheDocument();
  expect(screen.getByText("инициализация")).toBeInTheDocument();
  expect(screen.queryByText("вывод пуст")).toBeNull();
  expect(screen.getByTestId("tail-lines")).toHaveTextContent("");
  expect(termOpen).not.toHaveBeenCalled();
});

test("#612 input is not offered until a confirmed screen source has arrived", async () => {
  let release!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { release = resolve; })));
  open(); await tick();
  expect(termOpen).not.toHaveBeenCalled();
  await act(async () => { release(response("screen", ["ready"])); });
  expect(termOpen).toHaveBeenCalledTimes(1);
  expect(JSON.parse(String(termOpen.mock.calls[0][1].body)).mode).toBe('rw');
});

test("#612 a failed first read is not a confirmed live screen", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502 } as Response)));
  open(); await tick();
  expect(screen.getByRole("alert")).toHaveTextContent("панель не отвечает");
  expect(termOpen).not.toHaveBeenCalled();
});

test("#612 an unknown source is not silently treated as an interactive screen", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response("future-source", ["stage"])));
  open(); await tick();
  expect(termOpen).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("источник вывода неизвестен");
});

test("#612 a safe initialization reader refusal remains specific", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502,
    json: async () => ({ source: "initialization", error: "Nomad client is unreachable" }),
  }) as Response));
  open(); await tick();
  expect(screen.getByRole("alert")).toHaveTextContent("Nomad client is unreachable");
  expect(screen.queryByText(/панель не отвечает/)).toBeNull();
  expect(termOpen).not.toHaveBeenCalled();
});

test("#612 a real server failure still reports an error and keeps the last startup frame", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response("initialization", ["TASK [Install]"]))
    .mockResolvedValue({ ok: false, status: 502 } as Response));
  open(); await tick(); await tick(3000);
  expect(screen.getByTestId("tail-lines")).toHaveTextContent("TASK [Install]");
  expect(screen.getByRole("alert")).toHaveTextContent("панель не отвечает");
  expect(termOpen).not.toHaveBeenCalled();
});
