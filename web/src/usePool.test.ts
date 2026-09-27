// usePool (#297): первый снимок -- запросом, дальше -- кадры SSE; обрыв потока
// включает опрос, кадр -- выключает.
import { renderHook, act, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { usePool, POLL_MS } from "./usePool";

class FakeSource {
  static last: FakeSource | null = null;
  listeners: Record<string, (e: MessageEvent) => void> = {};
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { FakeSource.last = this; }
  addEventListener(name: string, fn: (e: MessageEvent) => void) { this.listeners[name] = fn; }
  close() { this.closed = true; }
  frame(data: unknown) { this.listeners["snapshot"]({ data: JSON.stringify(data) } as MessageEvent); }
}

const snap = (n: number) => ({ at: n, projects: [], nodes: [], usage: [], per_puppet: {}, per_user: [], journal: [], errors: [], creds: [],
  counts: { puppets: n, free: n, busy: 0, sick: 0, silent: 0, down: 0 } });

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  (globalThis as unknown as { EventSource: unknown }).EventSource = FakeSource;
  globalThis.fetch = vi.fn(async () => ({ json: async () => snap(1) })) as unknown as typeof fetch;
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

test("fetches first, then follows the stream, polls only while the stream is down", async () => {
  const { result, unmount } = renderHook(() => usePool());
  await waitFor(() => expect(result.current.snapshot?.counts.puppets).toBe(1));
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(FakeSource.last?.url).toBe("/events");
  expect(result.current.live).toBe(false);

  act(() => FakeSource.last!.frame(snap(2)));
  expect(result.current.snapshot?.counts.puppets).toBe(2);
  expect(result.current.live).toBe(true);

  act(() => FakeSource.last!.onerror!());
  expect(result.current.live).toBe(false);
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS + 1); });
  expect(fetch).toHaveBeenCalledTimes(2);

  act(() => FakeSource.last!.frame(snap(3)));
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 2); });
  expect(fetch).toHaveBeenCalledTimes(2);

  unmount();
  expect(FakeSource.last?.closed).toBe(true);
});
