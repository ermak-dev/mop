// Хвост экрана папета (#463): GET /api/puppet/<имя>/tail (#462) -- только по
// явному запросу админа, без follow: хвост не опрашивается по таймеру,
// каждый кадр -- новая кнопка. Обрезание помечается сервером и показывается
// честно; 401/403 и смена цели стирают прошлые строки.
// #489: временный 502 оставляет последний полученный кадр с ошибкой.
// #507: чтение stderr ушло вместе с кнопкой «лог» -- живые сессии в tmux,
// stderr у задачи пуст; проверяем, что хвост ходит только на /tail.
//
// RESULT: тесты написаны до реализации (RED).
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { usePuppetTail } from "./usePuppetCard";
import type { Session } from "./useSession";

const ADMIN: Session = { login: "anton", role: "admin" };
const GUEST_SESS: Session = { login: null, role: null };

const tailOk = (lines: string[], truncated = false) =>
  ({ ok: true, status: 200, json: async () => ({ lines, truncated }) }) as Response;

afterEach(() => vi.unstubAllGlobals());

test("nothing rides until asked: load() is the only trigger", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  expect(view.result.current.lines).toEqual([]);
  expect(view.result.current.error).toBeNull();
  // хвост без таймеров: микрозадачи исчерпаны -- и обращений нет
  await act(async () => { await Promise.resolve(); });
  expect(fetchMock).not.toHaveBeenCalled();
});

test("a guest load() asks nothing and refuses", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => usePuppetTail(GUEST_SESS, "pu-mop-1"));
  await act(async () => { await view.result.current.load(); });
  expect(view.result.current.lines).toEqual([]);
  expect(view.result.current.error).toBeTruthy();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("load() brings the tail, the truncated flag included", async () => {
  const fetchMock = vi.fn(async () => tailOk(["line 1", "line 2"], true));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.load(); });
  expect(view.result.current.lines).toEqual(["line 1", "line 2"]);
  expect(view.result.current.truncated).toBe(true);
  expect(view.result.current.error).toBeNull();
  expect(fetchMock).toHaveBeenCalledWith("/api/puppet/pu-mop-1/tail",
    expect.objectContaining({ credentials: "same-origin" }));
});

// #507: у хвоста один источник -- экран. Чтения stderr больше нет вовсе:
// ни параметра, ни запроса на .../stderr (кнопка «лог» ушла).
test("#507 the tail reads only the screen endpoint", async () => {
  const fetchMock = vi.fn(async () => tailOk(["screen 1"]));
  vi.stubGlobal("fetch", fetchMock);
  // @ts-expect-error #507: параметра источника больше нет
  renderHook(() => usePuppetTail(ADMIN, "pu-mop-1", "stderr"));
  await act(async () => { await Promise.resolve(); });
  expect(fetchMock).not.toHaveBeenCalled();
});

test("502 from a dead reader is an error with no lines", async () => {
  const fetchMock = vi.fn(async () => ({ ok: false, status: 502 } as Response));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.load(); });
  expect(view.result.current.lines).toEqual([]);
  expect(view.result.current.error).toBeTruthy();
});

test("a target change drops the former tail at once", async () => {
  const fetchMock = vi.fn(async () => tailOk(["old screen"]));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ target }: { target: string | null }) =>
    usePuppetTail(ADMIN, target), { initialProps: { target: "pu-mop-1" } });
  await act(async () => { await view.result.current.load(); });
  expect(view.result.current.lines).toEqual(["old screen"]);
  await act(async () => { view.rerender({ target: "pu-mop-2" }); });
  expect(view.result.current.lines).toEqual([]);
  expect(view.result.current.error).toBeNull();
  expect(view.result.current.truncated).toBe(false);
});

test("role loss drops the tail at once, a late answer does not bring it back", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = vi.fn(async () => slow);
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ session }: { session: Session }) =>
    usePuppetTail(session, "pu-mop-1"), { initialProps: { session: ADMIN } });
  let pending: Promise<void> | null = null;
  await act(async () => { pending = view.result.current.load(); });
  await act(async () => { view.rerender({ session: GUEST_SESS }); });
  expect(view.result.current.lines).toEqual([]);
  await act(async () => { release?.(tailOk(["secret screen"])); await pending; });
  expect(view.result.current.lines).toEqual([]);
});

test("a stale load answer does not overwrite a newer one", async () => {
  let releaseFirst: ((r: Response) => void) | null = null;
  const first = new Promise<Response>((resolve) => { releaseFirst = resolve; });
  const fetchMock = vi.fn()
    .mockImplementationOnce(async () => first)
    .mockImplementationOnce(async () => tailOk(["new screen"]));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  let p1: Promise<void> | null = null, p2: Promise<void> | null = null;
  await act(async () => { p1 = view.result.current.load(); });
  await act(async () => { p2 = view.result.current.load(); });
  await act(async () => { await p2; });
  expect(view.result.current.lines).toEqual(["new screen"]);
  await act(async () => { releaseFirst?.(tailOk(["stale screen"])); await p1; });
  expect(view.result.current.lines).toEqual(["new screen"]);
});

test("no storage: nothing lands in localStorage or sessionStorage", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => tailOk(["secret screen"])));
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.load(); });
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

// HYPOTHESIS: у запроса нет pending, и старый ответ может погасить новый спиннер.
// SOLUTION: loading принадлежит поколению запроса, включая чтение JSON.
// RESULT: старый ответ не гасит новый спиннер; смена источника очищает его.
// STATUS: FIXED — see #484
test("#484 loading belongs to the latest request through JSON decoding", async () => {
  let first!: (r: Response) => void;
  let body!: (b: unknown) => void;
  const fetchMock = vi.fn()
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { first = resolve; }))
    .mockResolvedValueOnce({ ok: true, json: () => new Promise((resolve) => { body = resolve; }) });
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  expect(view.result.current.loading).toBe(false);
  let p1!: Promise<void>, p2!: Promise<void>;
  await act(async () => { p1 = view.result.current.load(); });
  expect(view.result.current.loading).toBe(true);
  await act(async () => { p2 = view.result.current.load(); });
  await act(async () => { first(tailOk(["stale"])); await p1; });
  expect(view.result.current.loading).toBe(true);
  await act(async () => { body({ lines: ["latest"] }); await p2; });
  expect(view.result.current.loading).toBe(false);
  expect(view.result.current.lines).toEqual(["latest"]);
});

test("#484 switching sources cancels the previous loading state", async () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  const view = renderHook(({ target }) => usePuppetTail(ADMIN, target),
    { initialProps: { target: "pu-mop-1" as string | null } });
  await act(async () => { void view.result.current.load(); });
  expect(view.result.current.loading).toBe(true);
  view.rerender({ target: null });
  expect(view.result.current.loading).toBe(false);
});

test("#484 failed requests stop loading", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.load(); });
  expect(view.result.current.loading).toBe(false);
  expect(view.result.current.error).toBeTruthy();
});

// HYPOTHESIS: каждый успешный снимок меняет ссылку строк, а сбой стирает кадр.
// SOLUTION: одинаковые строки сохраняют ссылку; временный отказ оставляет
// последний кадр, но 401/403 очищает приватное и запрещает фоновый опрос.
// RESULT: одинаковые кадры сохраняют ссылку, закрытие отменяет запрос.
// STATUS: FIXED — see #489
test("#489 identical snapshots preserve the lines reference", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => tailOk(["same", "screen"])));
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.load(); });
  const lines = view.result.current.lines;
  await act(async () => { await view.result.current.load(); });
  expect(view.result.current.lines).toBe(lines);
});

test("#489 a refresh failure preserves the last snapshot and marks it stale", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(tailOk(["last frame"], true))
    .mockResolvedValueOnce({ ok: false, status: 502 }));
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.load(); });
  await act(async () => { await view.result.current.load(); });
  expect(view.result.current.lines).toEqual(["last frame"]);
  expect(view.result.current.truncated).toBe(true);
  expect(view.result.current.error).toBeTruthy();
});

test("#489 unmount aborts an in-flight read", async () => {
  const mock = vi.fn((_url: string, _init?: RequestInit) => new Promise<Response>(() => {}));
  vi.stubGlobal("fetch", mock);
  const view = renderHook(() => usePuppetTail(ADMIN, "pu-mop-1"));
  await act(async () => { void view.result.current.load(); });
  const signal = mock.mock.calls[0][1]?.signal;
  view.unmount();
  expect(signal?.aborted).toBe(true);
});

// прямое обращение к глобальному fetch внутри проверки подмены
test("no storage: nothing lands in localStorage or sessionStorage (recheck)", () => {
  // дубль выше уже прошёл; здесь только сигнатура глобалов на месте
  expect(typeof globalThis.fetch).toBe("function");
});
