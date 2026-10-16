// Рестарт (#465 -> #522): движок без кожи и без собственной моды --
// визард шлёт приказ-токен {act: "restart", params: {}} с финального
// экрана (#522: единое правило подтверждения), движок шлёт POST и
// поднимает стейт операции вверх (onState {status, error, lost}) --
// экран хода рисует визард. Слова вердикта -- модель #476: unknown --
// не failed, автоповтора нет. Никакого force. Дизейбл #508 живёт в
// пункте визарда (restartReason), не в движке.
//
// RESULT: тесты написаны до реализации (RED).
import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { RestartBox, restartReason } from "./RestartBox";
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
const opJson = (status: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "restart",
     target: "pu-mop-1", status }) }) as Response;
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (_url: string | URL, init?: { method?: string }) =>
    (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Приказ визарда (#522): подтверждение с финального экрана. */
class Order {
  current: { at: number; act: string; params: Record<string, unknown> } | null = null;
  fire(act: string, params: Record<string, unknown> = {}) {
    this.current = { at: Date.now() + Math.random(), act, params };
  }
}

/** Движок без кожи #522: рендерит null, стейт уходит наверх. */
const mountEngine = (order: Order, row: Puppet = ROW,
                     onState = vi.fn()) => renderUi(
  <RestartBox session={ADMIN} row={row} order={order.current} onState={onState} />);

/** Приказ и ререндер одним ходом. */
const send = (view: ReturnType<typeof mountEngine>, order: Order,
              onState = vi.fn()) => {
  order.fire("restart");
  // ререндер держит ту же форму дерева, что монтировал renderUi: смена
  // корня (голый движок вместо MantineProvider) перемонтирует движок,
  // и useOrder видит приказ уже «виденным» на свежем mount -- выстрела
  // нет. Визард в проде ререндерится внутри того же дерева.
  view.rerender(<MantineProvider><RestartBox session={ADMIN} row={ROW}
    order={order.current} onState={onState} /></MantineProvider>);
};

test("#522 the engine renders no dialog, state goes up", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  send(view, order, onState);
  // движок нем: ни моды, ни кнопок -- экран хода рисует визард
  expect(screen.queryByRole("dialog")).toBeNull();
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "accepted" })));
});

test("#522 the order posts the exact restart body", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops",
    expect.objectContaining({ method: "POST" })));
  const body = JSON.parse((fetchMock.mock.calls[0][1] as { body: string }).body);
  expect(body).toEqual({ action: "restart", target: "pu-mop-1", params: {} });
});

test("#522 a failed verdict carries the server reason up", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(
      ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "restart",
         target: "pu-mop-1", status: "failed", error: "alloc refused" }) }) as Response));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  send(view, order, onState);
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "failed", error: "alloc refused" })));
});

test("#522 double order while live rides one POST", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order);
  send(view, order);
  await waitFor(() => { release?.(ok202("op-9")); });
  await waitFor(() => expect(fetchMock.mock.calls
    .filter((c) => (c[1] as { method?: string })?.method === "POST")).toHaveLength(1));
});

test("#522 no storage: nothing lands in localStorage or sessionStorage", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order);
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

// «Новая команда» (#522): визард просит движок забыть вердикт -- стейт
// уходит в idle и поднимается наверх, иначе pick того же пункта
// отвлекался бы на старый терминал.
test("#522 a forget signal settles the engine back to idle", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("failed")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  send(view, order, onState);
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "failed" })));
  // forget-токен (#522): та же форма дерева, иначе ремоунт подделал бы idle
  const forget: { at: number; act: string; params: Record<string, unknown> } =
    { at: Date.now() + Math.random(), act: "forget", params: {} };
  view.rerender(<MantineProvider><RestartBox session={ADMIN} row={ROW}
    order={order.current} forget={forget} onState={onState} /></MantineProvider>);
  await waitFor(() => expect(onState.mock.calls[onState.mock.calls.length - 1][0])
    .toMatchObject({ status: "idle" }));
  // забытый вердикт не воскресает: нового fetch нет
  expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(2);
});

// ── #508: признак строки -- чистая функция, пункт визарда её зовёт ──────

test("#508 restartReason names down and failing with human words", () => {
  const DOWN: Puppet = { ...ROW, alloc_status: "stopped", bucket: "down",
    kind: "unknown", state: "stopped" };
  const FAILING: Puppet = { ...ROW, kind: "failing",
    state: "FAILED: exit 1", detail: "exit 1" };
  expect(restartReason(DOWN)).toBe("рестарт недоступен: не запущен");
  expect(restartReason(FAILING)).toBe("рестарт недоступен: задача падает");
  expect(restartReason(ROW)).toBeNull();
});
