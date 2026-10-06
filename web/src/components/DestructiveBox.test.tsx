// Разрушительные действия (#473 -> #522): recycle/delete/wipe -- три
// отдельных действия, движок без кожи и без собственной моды. Визард
// шлёт приказ-токен с финального экрана, движок шлёт POST и поднимает
// стейт наверх (onState {status, error, lost}) -- экран хода рисует
// визард. Stale-защита движковая (#473): смена источника стирает
// операцию в тот же рендер (useAdminOp). Частичный исход --
// «частично», recovery не обещается. Никакого force.
//
// RESULT: тесты написаны до реализации (RED).
import { act, cleanup, waitFor } from "@testing-library/react";
import { screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { DestructiveBox } from "./DestructiveBox";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

const ROW: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "busy: #12", kind: "busy", bucket: "busy", detail: null,
  branch: "#12", owner: "ermak", llm: "glm",
  origin: "git@h:g/mop.git", disk_kb: null,
};

const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string, action = "recycle", error: string | null = null) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action,
     target: "pu-mop-1", status, error }) }) as Response;
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) =>
    (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Приказ визарда (#522): «Завершить» с финального экрана. */
class Order {
  current: { at: number; act: string; params: Record<string, unknown> } | null = null;
  fire(act: string, params: Record<string, unknown> = {}) {
    this.current = { at: Date.now() + Math.random(), act, params };
  }
}

/** Движок без кожи #522. */
const mountEngine = (order: Order, row: Puppet = ROW,
                     onState = vi.fn()) => renderUi(
  <DestructiveBox session={ADMIN} row={row} order={order.current} onState={onState} />);

/** Последний POST-вызов fetch как разобранный body. */
const lastPost = (fetchMock: ReturnType<typeof postGet>) => {
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  return post ? JSON.parse((post[1] as { body: string }).body) : null;
};

/** Ререндер с новым приказом: как визард шлёт подтверждение. */
const sendOrder = (view: ReturnType<typeof mountEngine>, order: Order,
                   row: Puppet = ROW, onState = vi.fn()) => {
  // ререндер держит ту же форму дерева, что монтировал renderUi: смена
  // корня перемонтирует движок, и useOrder глотает приказ (mount молчит)
  view.rerender(<MantineProvider><DestructiveBox session={ADMIN} row={row}
    order={order.current} onState={onState} /></MantineProvider>);
};

test("#522 no buttons and no dialog in the engine skin, entry by order only", () => {
  vi.stubGlobal("fetch", vi.fn());
  const order = new Order();
  mountEngine(order);
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
  // первый рендер молчит: mount-выстрел был бы ложным стартом
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

test("#522 the recycle order posts the exact action once, state goes up", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  order.fire("recycle");
  sendOrder(view, order, ROW, onState);
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "accepted" })));
  expect(lastPost(fetchMock)).toEqual({ action: "recycle", target: "pu-mop-1", params: {} });
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops", expect.objectContaining({
    method: "POST", credentials: "same-origin" }));
});

test("#522 delete and wipe orders post their own actions", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded", "delete")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  order.fire("delete");
  sendOrder(view, order);
  await waitFor(() => expect(lastPost(fetchMock)?.action).toBe("delete"));
  // первый ход дошёл до терминала: GET-вердикт «succeeded» усвоен, иначе
  // движковый дедуп (живой id) честно съел бы второй приказ -- живой ход
  // одна команда (#476), новая команда -- после вердикта
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops/op-1",
    expect.objectContaining({ credentials: "same-origin" })));
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  order.fire("wipe");
  sendOrder(view, order);
  const posts = fetchMock.mock.calls
    .filter((c) => (c[1] as { method?: string })?.method === "POST");
  await waitFor(() => expect(posts.length).toBeGreaterThanOrEqual(2));
  expect(JSON.parse((posts[posts.length - 1][1] as { body: string }).body).action)
    .toBe("wipe");
});

test("#522 a failed verdict carries the reason up, never a modal", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("failed", "wipe", "image builder refused")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  order.fire("wipe");
  sendOrder(view, order, ROW, onState);
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "failed", error: "image builder refused" })));
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("#522 double submit rides one POST", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  order.fire("recycle");
  sendOrder(view, order);
  order.fire("recycle");
  sendOrder(view, order);
  await act(async () => { release?.(ok202("op-9")); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("#522 unknown is named unknown up the wire, never failed", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown", "wipe")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  order.fire("wipe");
  sendOrder(view, order, ROW, onState);
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "unknown" })));
  const calls = onState.mock.calls.map((c) => c[0]?.status);
  expect(calls).not.toContain("failed");
});
