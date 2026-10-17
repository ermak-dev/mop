// Разрушительные действия (#473 -> #522): recycle/delete/wipe -- три
// отдельных действия, движок без кожи и без собственной моды. Визард
// шлёт приказ-токен с финального экрана, движок шлёт POST и поднимает
// стейт наверх (onState {status, error, lost}) -- экран хода рисует
// визард. Stale-защита движковая (#473): смена источника стирает
// операцию в тот же рендер (useAdminOp). Частичный исход --
// «частично», recovery не обещается. Никакого force.
// #627: через общий store -- delete и wipe на свои маршруты контроллера
// /api/v1/puppets/<id>/commands/<действие>; recycle контроллер ещё не принимает
// (#625 ждёт #643) и отказывается до POST.
import { act, cleanup, waitFor } from "@testing-library/react";
import { screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { DestructiveBox } from "./DestructiveBox";
import { renderUi } from "../test-utils";
import { ADMIN, ID, deferred, networkFixture, reply } from "../network-test-utils";
import type { Puppet } from "../types";

const ROW: Puppet = {
  name: ID, node: "node", alloc_status: "running",
  state: "busy: #12", kind: "busy", bucket: "busy", detail: null,
  branch: "#12", owner: "ermak", llm: "glm",
  origin: "git@h:g/mop.git", disk_kb: null,
};

let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); });
afterEach(() => { net.stop(); cleanup(); vi.unstubAllGlobals(); });

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

/** Ререндер с новым приказом: как визард шлёт подтверждение. */
const sendOrder = (view: ReturnType<typeof mountEngine>, order: Order,
                   row: Puppet = ROW, onState = vi.fn()) => {
  // ререндер держит ту же форму дерева, что монтировал renderUi: смена
  // корня перемонтирует движок, и useOrder глотает приказ (mount молчит)
  view.rerender(<MantineProvider><DestructiveBox session={ADMIN} row={row}
    order={order.current} onState={onState} /></MantineProvider>);
};
const finish = async (state: string, reason = "") => {
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => String(url).startsWith("/api/v1/operations")
    ? Promise.resolve(reply(net.operation(state, "3", reason))) : base(url, init));
  await net.invalidateOperation();
};

test("#522 no buttons and no dialog in the engine skin, entry by order only", async () => {
  await net.start();
  const order = new Order();
  mountEngine(order);
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
  // первый рендер молчит: mount-выстрел был бы ложным стартом
  expect(net.posts()).toHaveLength(0);
});

test("#522 recycle is refused before any POST until the controller admits it", async () => {
  await net.start();
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  order.fire("recycle");
  sendOrder(view, order, ROW, onState);
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "refused", error: expect.stringContaining("not implemented by the controller") })));
  expect(net.posts()).toHaveLength(0);
});

test("#522 delete and wipe orders post their own actions", async () => {
  await net.start();
  const order = new Order();
  const view = mountEngine(order);
  order.fire("delete");
  sendOrder(view, order);
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  expect(net.posts()[0][0]).toBe(`/api/v1/puppets/${ID}/commands/delete`);
  expect(JSON.parse(String(net.posts()[0][1]?.body)).params).toEqual({});
  // первый ход дошёл до терминала, иначе живой ход съел бы второй приказ:
  // живой ход одна команда (#476), новая команда -- после вердикта
  await finish("succeeded");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  order.fire("wipe");
  sendOrder(view, order);
  await waitFor(() => expect(net.posts()).toHaveLength(2));
  expect(net.posts()[1][0]).toBe(`/api/v1/puppets/${ID}/commands/wipe`);
  expect(net.posts()[1][1]).toMatchObject({ method: "POST", credentials: "same-origin" });
});

test("#522 a failed verdict carries the reason up, never a modal", async () => {
  await net.start();
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  order.fire("wipe");
  sendOrder(view, order, ROW, onState);
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  await finish("failed", "session is alive");
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "failed", error: "session is alive" })));
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("#522 double submit rides one POST", async () => {
  await net.start();
  const slow = deferred(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => {
    if (init?.method === "POST") { net.capture(init); return slow.promise; }
    return base(url, init);
  });
  const order = new Order();
  const view = mountEngine(order);
  order.fire("wipe");
  sendOrder(view, order);
  order.fire("wipe");
  sendOrder(view, order);
  await act(async () => { slow.resolve(reply(net.operation("accepted", "1"), 202)); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(net.posts()).toHaveLength(1);
});

test("#522 unknown is named unknown up the wire, never failed", async () => {
  await net.start();
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ROW, onState);
  order.fire("wipe");
  sendOrder(view, order, ROW, onState);
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  await finish("unknown");
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "unknown" })));
  const calls = onState.mock.calls.map((c) => c[0]?.status);
  expect(calls).not.toContain("failed");
});
