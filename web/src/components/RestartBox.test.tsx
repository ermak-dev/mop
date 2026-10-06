// Рестарт (#465 -> #520): движок без кожи, вход -- приказ визарда
// {act: "restart", params: {}}: клик по пункту первого экрана шлёт POST
// сразу (#513), Stepper не переключается. Вердикт -- словами #476,
// unknown -- не failed, автоповтора нет. Никакого force. Дизейбл #508
// живёт в пункте визарда (restartReason), не в движке.
//
// RESULT: тесты написаны до реализации (RED).
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { RestartBox, restartReason } from "./RestartBox";
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
const opJson = (status: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "restart",
     target: "pu-mop-1", status }) }) as Response;
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) =>
    (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Приказ визарда (#520): клик по пункту рестарта. */
class Order {
  current: { at: number; act: string; params: Record<string, unknown> } | null = null;
  fire(act: string, params: Record<string, unknown> = {}) {
    this.current = { at: Date.now() + Math.random(), act, params };
  }
}

/** Движок в коже #520: без кнопки, вход -- приказ-токен. */
function mountEngine(order: Order, row: Puppet = ROW, session: Session = ADMIN) {
  return renderUi(<RestartBox session={session} row={row} order={order.current} />);
}

/** Приказ и ререндер одним ходом. */
const send = (view: ReturnType<typeof mountEngine>, order: Order) => {
  order.fire("restart");
  view.rerender(<MantineProvider>
    <RestartBox session={ADMIN} row={ROW} order={order.current} />
  </MantineProvider>);
};

test("#520 the restart order posts at once, no confirm dialog", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order);
  expect(screen.queryByText(/подтвердите рестарт/i)).toBeNull();
  const body = JSON.parse((fetchMock.mock.calls[0][1] as { body: string }).body);
  expect(body).toEqual({ action: "restart", target: "pu-mop-1", params: {} });
  await waitFor(() => expect(screen.getByText(/идёт/i)).toBeInTheDocument());
});

test("#520 terminal succeeded verdict is shown; retry needs a new order", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order);
  await waitFor(() => expect(screen.getByText(/перезапущен/i)).toBeInTheDocument());
});

test("#520 unknown is named unknown, never failed", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order);
  await waitFor(() => expect(screen.getByText(/неизвестен/i)).toBeInTheDocument());
  expect(screen.queryByText(/не удался/i)).toBeNull();
});

test("#520 a refused operation names the server phrase, retry posts again", async () => {
  const fetchMock = postGet(
    () => Promise.resolve({ ok: false, status: 409,
      json: async () => ({ error: "another operation is queued" }) }) as Response,
    () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order);
  await waitFor(() => expect(screen.getByText(/queued/i)).toBeInTheDocument());
  // Повтор начинается из результата («новая команда» шлёт снова): клик
  // подтверждения больше не нужен, рестарт обратим (#513).
  fireEvent.click(screen.getByRole("button", { name: "новая команда" }));
  await waitFor(() =>
    expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
      .toHaveLength(2));
});

test("#520 no storage: nothing lands in localStorage or sessionStorage", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order);
  await waitFor(() => expect(screen.getByText(/перезапущен/i)).toBeInTheDocument());
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
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
