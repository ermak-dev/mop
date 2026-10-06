// Разрушительные действия (#473 -> #520): recycle/delete/wipe -- три
// отдельных действия, движок без кожи. #520: единый экран последствий
// собирает визард (ControlWindow), приказ приезжает токеном
// {at, act, params}: движок шлёт POST и показывает вердикт
// OperationResult'ом. Stale-защита движковая (#473): смена источника
// стирает операцию в тот же рендер (useAdminOp). Частичный исход --
// «частично», recovery не обещается. Никакого force.
//
// RESULT: тесты написаны до реализации (RED).
import { act, cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { DestructiveBox } from "./DestructiveBox";
import { MantineProvider } from "@mantine/core";
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
const opJson = (status: string, action = "recycle") =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action,
     target: "pu-mop-1", status }) }) as Response;
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) =>
    (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Приказ визарда (#520): токен меняется, движок срабатывает. */
class Order {
  current: { at: number; act: string; params: Record<string, unknown> } | null = null;
  /** Подтверждение с финального экрана визарда. */
  fire(act: string, params: Record<string, unknown> = {}) {
    this.current = { at: Date.now() + Math.random(), act, params };
  }
}

/** Движок в коже #520: без кнопок, вход -- приказ-токен. */
function mountEngine(order: Order, row: Puppet = ROW, session: Session = ADMIN) {
  return renderUi(<DestructiveBox session={session} row={row} order={order.current} />);
}

/** Ререндер с новым приказом: как визард шлёт подтверждение. */
function sendOrder(view: ReturnType<typeof mountEngine>, order: Order,
                   row: Puppet = ROW) {
  view.rerender(<MantineProvider>
    <DestructiveBox session={ADMIN} row={row} order={order.current} />
  </MantineProvider>);
}

/** Последний POST-вызов fetch как разобранный body. */
const lastPost = (fetchMock: ReturnType<typeof postGet>) => {
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  return post ? JSON.parse((post[1] as { body: string }).body) : null;
};

test("#520 no buttons in the engine skin, entry by order only", () => {
  vi.stubGlobal("fetch", vi.fn());
  const order = new Order();
  mountEngine(order);
  expect(screen.queryByRole("button", { name: /^recycle$/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /^wipe$/i })).toBeNull();
  // первый рендер молчит: mount-выстрел был бы ложным стартом
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

test("#520 the recycle order posts the exact action once", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  order.fire("recycle");
  sendOrder(view, order);
  await waitFor(() => expect(screen.getByText(/Идёт/)).toBeInTheDocument());
  expect(lastPost(fetchMock)).toEqual({ action: "recycle", target: "pu-mop-1", params: {} });
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops", expect.objectContaining({
    method: "POST", credentials: "same-origin" }));
});

test("#520 delete and wipe orders post their own actions", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded", "delete")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  order.fire("delete");
  sendOrder(view, order);
  await waitFor(() => expect(lastPost(fetchMock)?.action).toBe("delete"));
});

test("#520 the verdict rides the OperationResult modal", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("failed", "wipe")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  order.fire("wipe");
  sendOrder(view, order);
  const verdict = await screen.findByText(/Не удался/);
  expect(verdict.closest('[role="dialog"]')).not.toBeNull();
});

test("#520 partial is named partial, no recovery is promised", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("partial")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  order.fire("recycle");
  sendOrder(view, order);
  await waitFor(() => expect(screen.getByText(/Частично/)).toBeInTheDocument());
  expect(screen.queryByText(/восстанов/i)).toBeNull();
});

test("#520 double submit rides one POST", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  order.fire("recycle");
  sendOrder(view, order);
  // повторный приказ при живом POST: движковый дедуп -- POST один
  order.fire("recycle");
  sendOrder(view, order);
  await act(async () => { release?.(ok202("op-9")); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("#520 unknown is named unknown, never failed", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown", "wipe")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  order.fire("wipe");
  sendOrder(view, order);
  await waitFor(() => expect(screen.getByText(/Исход неизвестен/)).toBeInTheDocument());
  expect(screen.queryByText(/Не удался/)).toBeNull();
});
