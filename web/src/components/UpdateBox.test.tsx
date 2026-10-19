// Обновление (#470 -> #522): движок без кожи и без собственной моды --
// визард шлёт приказ-токен {act: "update", params: {fresh[, origin]}}
// с финального экрана (#522: единое правило подтверждения -- финал у
// всех, включая keep), движок поднимает стейт операции наверх
// (onState {status, error, lost}). «Новая команда» на экране хода
// визарда возвращает к выбору; ордер здесь -- всегда новая команда,
// терминальный ход не глотается.
// #627: общий store контроллера. Обновление контроллер ещё не принимает
// (#625 ждёт композиции запуска #643): приказ отказывается до POST и
// называет причину, ни fresh, ни origin не уходят в сеть.
import { act, cleanup, waitFor } from "@testing-library/react";
import { screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { UpdateBox } from "./UpdateBox";
import { renderUi } from "../test-utils";
import { ADMIN, ID, networkFixture } from "../network-test-utils";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ROW: Puppet = {
  name: ID, node: "node", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
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
const mountEngine = (order: Order, session: Session = ADMIN,
                     onState = vi.fn()) => renderUi(
  <UpdateBox session={session} row={ROW} order={order.current} onState={onState} />);

/** Приказ и ререндер одним ходом: как визард шлёт подтверждение. */
const send = (view: ReturnType<typeof mountEngine>, order: Order,
              act: string, params: Record<string, unknown>,
              onState = vi.fn()) => {
  order.fire(act, params);
  // ререндер держит ту же форму дерева, что монтировал renderUi: смена
  // корня перемонтирует движок, и useOrder глотает приказ (mount молчит)
  view.rerender(<MantineProvider><UpdateBox session={ADMIN} row={ROW}
    order={order.current} onState={onState} /></MantineProvider>);
};
const refusal = expect.objectContaining({ status: "refused",
  error: expect.stringContaining("not implemented by the controller") });

test("#522 the engine renders no dialog and stays silent on mount", async () => {
  await net.start();
  mountEngine(new Order());
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(net.posts()).toHaveLength(0);
});

test.each([[{ fresh: false }], [{ fresh: true, origin: "https://x/y/other.git" }]])(
  "#627 update %j is refused before any POST with the reason going up", async (params) => {
    await net.start();
    const order = new Order();
    const onState = vi.fn();
    const view = mountEngine(order, ADMIN, onState);
    send(view, order, "update", params, onState);
    await waitFor(() => expect(onState).toHaveBeenCalledWith(refusal));
    expect(net.posts()).toHaveLength(0);
    expect(screen.queryByRole("dialog")).toBeNull();
    for (const [url] of net.http.mock.calls) expect(String(url)).not.toContain("other.git");
  });

test("#522 a guest engine is silent", () => {
  vi.stubGlobal("fetch", vi.fn());
  mountEngine(new Order(), { login: null, role: null });
  expect(screen.queryByRole("button", { name: /обнов/i })).toBeNull();
  expect(vi.mocked(fetch)).not.toHaveBeenCalled();
});

// #487/#513: ордер здесь -- всегда новая команда с финального экрана
// визарда; терминальный отказ не глотает следующий приказ.
test("#522 an order after a terminal refusal is a new command and is refused again", async () => {
  await net.start();
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ADMIN, onState);
  send(view, order, "update", { fresh: false }, onState);
  await waitFor(() => expect(onState).toHaveBeenCalledWith(refusal));
  const first = onState.mock.calls.length;
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  send(view, order, "update", { fresh: true }, onState);
  // #718: новый callback может быть submitted; терминальный барьер --
  // обе прежние assertions вместе, не sleep и не увеличение timeout.
  await waitFor(() => {
    expect(onState.mock.calls.length).toBeGreaterThan(first);
    expect(onState.mock.calls.at(-1)?.[0]).toEqual(refusal);
  });
  expect(net.posts()).toHaveLength(0);
});
