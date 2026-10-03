// Хук операции админа (#465): POST /api/admin/ops {action,target,params}
// -> 202 {id} -> опрос GET /api/admin/ops/<id> до терминала. Контракт
// #461/#476: unknown -- НЕ failed, автоповтора нет; двойной под -- тот же
// id; потерянный первый ответ -- unknown; смена источника (роль, цель)
// стирает операцию в тот же рендер, опоздавший 202 не воскресит её.
// Результат живёт в памяти страницы: перезагрузка начинает заново -- id
// хранить негде (storage запрещён #463, списка своих операций в контракте
// нет); это первый выпуск, названо в отчёте.
//
// HYPOTHESIS (#465): в карточке нет кнопки restart, а отметка running в
// публичном ростере не подтверждает операцию.
// RESULT: тесты написаны до реализации (RED).
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useAdminOp } from "./useAdminOp";
import type { Session } from "./useSession";

const ADMIN: Session = { login: "anton", role: "admin" };
const GUEST_SESS: Session = { login: null, role: null };

const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "restart",
     target: "pu-mop-1", status }) }) as Response;
const pause = (ms = 5) => new Promise((r) => setTimeout(r, ms));

/** POST -- первый вызов, GET результата -- остальные. */
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) =>
    (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => vi.unstubAllGlobals());

test("no request rides until submit", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  await pause();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("submit posts the narrow schema and polls to the terminal status", async () => {
  let gets = 0;
  const fetchMock = postGet(
    () => Promise.resolve(ok202("op-1")),
    () => { gets += 1; return Promise.resolve(opJson(gets === 1 ? "running" : "succeeded")); });
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.submit(); });
  await waitFor(() => expect(view.result.current.op.status).toBe("succeeded"));
  expect(view.result.current.op.id).toBe("op-1");
  expect(view.result.current.op.error).toBeNull();
  // узкая схема: ни subject/verb/shell, ни лишних полей
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops", expect.objectContaining({
    method: "POST", credentials: "same-origin",
  }));
  const body = JSON.parse((fetchMock.mock.calls[0][1] as { body: string }).body);
  expect(body).toEqual({ action: "restart", target: "pu-mop-1", params: {} });
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops/op-1",
    expect.objectContaining({ credentials: "same-origin" }));
  // терминал -- опрос остановлен
  const after = fetchMock.mock.calls.length;
  await pause(15);
  expect(fetchMock.mock.calls.length).toBe(after);
});

test("double submit rides once and yields the same id", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("queued")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  let p1: Promise<string | null> | null = null, p2: Promise<string | null> | null = null;
  await act(async () => { p1 = view.result.current.submit(); });
  await act(async () => { p2 = view.result.current.submit(); });
  await act(async () => { release?.(ok202("op-9")); });
  expect(await p1).toBe("op-9");
  expect(await p2).toBe("op-9");
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
  // и когда задача уже принята (queued), повтор -- тот же id без нового POST
  const id3 = await act(async () => await view.result.current.submit());
  expect(id3).toBe("op-9");
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("a late 202 of a former target does not start its poll", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ target } = { target: "pu-mop-1" as string | null }) =>
    useAdminOp(ADMIN, target), { initialProps: { target: "pu-mop-1" as string | null } });
  await act(async () => { view.result.current.submit(); });
  await act(async () => { view.rerender({ target: "pu-mop-2" }); });
  await act(async () => { release?.(ok202("op-old")); });
  await pause();
  // новая цель -- idle, опрос прежнего id не начался
  expect(view.result.current.op.status).toBe("idle");
  expect(fetchMock).not.toHaveBeenCalledWith("/api/admin/ops/op-old", expect.anything());
});

test("role loss clears the operation; a late 202 does not resurrect it", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ session } = { session: ADMIN }) =>
    useAdminOp(session, "pu-mop-1"), { initialProps: { session: ADMIN } });
  await act(async () => { view.result.current.submit(); });
  await act(async () => { view.rerender({ session: GUEST_SESS }); });
  expect(view.result.current.op.status).toBe("idle");
  await act(async () => { release?.(ok202("op-2")); });
  await pause();
  expect(view.result.current.op.status).toBe("idle");
});

test("unknown from the server is terminal and kept as unknown", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.submit(); });
  await waitFor(() => expect(view.result.current.op.status).toBe("unknown"));
  const after = fetchMock.mock.calls.length;
  await pause(15);
  expect(fetchMock.mock.calls.length).toBe(after);
});

test("a purged result (not found) reads as unknown", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
    () => Promise.resolve({ ok: true, status: 200,
      json: async () => ({ status: "not found" }) }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.submit(); });
  await waitFor(() => expect(view.result.current.op.status).toBe("unknown"));
});

test("three poll failures in a row read as unknown and stop the poll", async () => {
  let gets = 0;
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
    () => { gets += 1; return Promise.reject(new Error("down")); });
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1", 1));
  await act(async () => { await view.result.current.submit(); });
  await waitFor(() => expect(view.result.current.op.status).toBe("unknown"), { timeout: 2000 });
  expect(gets).toBeGreaterThanOrEqual(3);
  const after = fetchMock.mock.calls.length;
  await pause(15);
  expect(fetchMock.mock.calls.length).toBe(after);
});

test("a lost POST answer is unknown, and nothing retries it", async () => {
  const fetchMock = postGet(() => Promise.reject(new Error("connection reset")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1", 1));
  await act(async () => { await view.result.current.submit(); });
  await waitFor(() => expect(view.result.current.op.status).toBe("unknown"));
  expect(view.result.current.op.lost).toBe(true);
  expect(view.result.current.op.id).toBeNull();
  const after = fetchMock.mock.calls.length;
  await pause(20);
  // автоповтора нет: ни повторного POST, ни опроса без id
  expect(fetchMock.mock.calls.length).toBe(after);
});

test("a 202 without an id is a lost answer too", async () => {
  const fetchMock = postGet(() => Promise.resolve({ ok: true, status: 202,
    json: async () => ({}) }) as Response, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op.status).toBe("unknown");
  expect(view.result.current.op.lost).toBe(true);
});

test("a POST refusal names the phrase and a manual retry posts again", async () => {
  const fetchMock = postGet(() => Promise.resolve({ ok: false, status: 409,
    json: async () => ({ error: "another operation is queued for this target" }) }) as Response,
    () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op.status).toBe("refused");
  expect(view.result.current.op.error).toContain("queued");
  // повтор -- решение админа: второй POST уходит
  await act(async () => { await view.result.current.submit(); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(2);
});

test("an unregistered operation (404) reads as refused with the phrase", async () => {
  const fetchMock = postGet(() => Promise.resolve({ ok: false, status: 404,
    json: async () => ({ error: "unsupported operation" }) }) as Response,
    () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op.status).toBe("refused");
  expect(view.result.current.op.error).toContain("unsupported");
});

test("a non-admin submit asks nothing", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(GUEST_SESS, "pu-mop-1"));
  await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op.status).toBe("refused");
  expect(view.result.current.op.error).toBeTruthy();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("no storage: nothing lands in localStorage or sessionStorage", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1"));
  await act(async () => { await view.result.current.submit(); });
  await waitFor(() => expect(view.result.current.op.status).toBe("succeeded"));
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

// #469: send -- та же операция админа, но с параметрами: submit несёт
// {message}. Restart зовёт submit() без аргументов -- params: {} как был.
test("#469 send passes its params: submit carries the message", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useAdminOp(ADMIN, "pu-mop-1", "send"));
  await act(async () => { await view.result.current.submit("send", { message: "привет" }); });
  const body = JSON.parse((fetchMock.mock.calls[0][1] as { body: string }).body);
  expect(body).toEqual({ action: "send", target: "pu-mop-1", params: { message: "привет" } });
});
