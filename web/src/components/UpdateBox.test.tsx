// Обновление (#470 -> #522): движок без кожи и без собственной моды --
// визард шлёт приказ-токен {act: "update", params: {fresh[, origin]}}
// с финального экрана (#522: единое правило подтверждения -- финал у
// всех, включая keep), движок шлёт POST и поднимает стейт операции
// наверх (onState {status, error, lost}). «Новая команда» на экране
// хода визарда возвращает к выбору; ордер здесь -- всегда новая
// команда, терминальный ход не глотается.
//
// RESULT: тесты написаны до реализации (RED).
import { act, cleanup, waitFor } from "@testing-library/react";
import { screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { UpdateBox } from "./UpdateBox";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

const ROW: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};

const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string, error: string | null = null) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "update",
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
const mountEngine = (order: Order, session: Session = ADMIN,
                     onState = vi.fn()) => renderUi(
  <UpdateBox session={session} row={ROW} order={order.current} onState={onState} />);

/** Последний POST-вызов fetch как разобранный body. */
const lastPost = (fetchMock: ReturnType<typeof postGet>) => {
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  return post ? JSON.parse((post[1] as { body: string }).body) : null;
};

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

test("#522 the engine renders no dialog, accepted state goes up", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ADMIN, onState);
  send(view, order, "update", { fresh: false }, onState);
  expect(screen.queryByRole("dialog")).toBeNull();
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "accepted" })));
});

test("#522 keep posts fresh=false after the final screen", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "update", { fresh: false });
  await waitFor(() =>
    expect(lastPost(fetchMock)).toEqual({ action: "update", target: "pu-mop-1",
      params: { fresh: false } }));
});

test("#522 remove posts fresh=true with the new origin", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "update", { fresh: true, origin: "https://x/y/other.git" });
  await waitFor(() =>
    expect(lastPost(fetchMock)).toEqual({ action: "update", target: "pu-mop-1",
      params: { fresh: true, origin: "https://x/y/other.git" } }));
});

test("#522 a refused verdict carries the reason up, never a modal", async () => {
  const fetchMock = postGet(
    () => Promise.resolve(({ ok: false, status: 409,
      json: async () => ({ error: "another operation is queued" }) }) as Response),
    () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const onState = vi.fn();
  const view = mountEngine(order, ADMIN, onState);
  send(view, order, "update", { fresh: false }, onState);
  await waitFor(() => expect(onState).toHaveBeenCalledWith(
    expect.objectContaining({ status: "refused", error: "another operation is queued" })));
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("#522 double submit rides one POST", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "update", { fresh: true });
  send(view, order, "update", { fresh: true });
  await act(async () => { release?.(ok202("op-9")); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("#522 a guest engine is silent", () => {
  vi.stubGlobal("fetch", vi.fn());
  const order = new Order();
  mountEngine(order, { login: null, role: null });
  expect(screen.queryByRole("button", { name: /обнов/i })).toBeNull();
});

// #487/#513: ордер здесь -- всегда новая команда с финального экрана
// визарда; терминальный ход не глотается, дедуп живого хода --
// useAdminOp.
test("#522 an order after a terminal verdict is a new command and posts", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("failed", "review refusal")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "update", { fresh: false });
  await waitFor(() => expect(fetchMock.mock.calls
    .filter((c) => (c[1] as { method?: string })?.method === "POST")).toHaveLength(1));
  // первый ход дошёл до терминала (GET-вердикт «failed» усвоен): живой
  // ход -- одна команда, дедуп живого id не должен глотать новую
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops/op-1",
    expect.objectContaining({ credentials: "same-origin" })));
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  send(view, order, "update", { fresh: false });
  await waitFor(() =>
    expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
      .toHaveLength(2));
});
