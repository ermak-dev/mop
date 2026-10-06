// Обновление (#470 -> #520): три явных режима fresh -- keep/replace/
// remove, никаких умолчаний. #520: шаг параметров и единый финальный
// экран собирает визард (ControlWindow), приказ приезжает токеном
// {at, act, params: {fresh[, origin]}}: движок шлёт POST и показывает
// вердикт OperationResult'ом. keep -- обратим (#513): подтверждение не
// нужно, визард шлёт приказ сразу с шага параметров.
//
// RESULT: тесты написаны до реализации (RED).
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { UpdateBox } from "./UpdateBox";
import { MantineProvider } from "@mantine/core";
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

/** Приказ визарда (#520): подтверждение с финального экрана. */
class Order {
  current: { at: number; act: string; params: Record<string, unknown> } | null = null;
  fire(act: string, params: Record<string, unknown> = {}) {
    this.current = { at: Date.now() + Math.random(), act, params };
  }
}

/** Движок в коже #520: без кнопок, вход -- приказ-токен. */
function mountEngine(order: Order, row: Puppet = ROW, session: Session = ADMIN) {
  return renderUi(<UpdateBox session={session} row={row} order={order.current} />);
}

/** Последний POST-вызов fetch как разобранный body. */
const lastPost = (fetchMock: ReturnType<typeof postGet>) => {
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  return post ? JSON.parse((post[1] as { body: string }).body) : null;
};

/** Приказ и ререндер одним ходом: как визард шлёт подтверждение. */
const send = (view: ReturnType<typeof mountEngine>, order: Order,
              act: string, params: Record<string, unknown>) => {
  order.fire(act, params);
  view.rerender(<MantineProvider>
    <UpdateBox session={ADMIN} row={ROW} order={order.current} />
  </MantineProvider>);
};

test("#520 keep posts fresh=false at once, no confirm", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "update", { fresh: false });
  expect(screen.queryByText(/подтвердите update/i)).toBeNull();
  await waitFor(() =>
    expect(lastPost(fetchMock)).toEqual({ action: "update", target: "pu-mop-1",
      params: { fresh: false } }));
});

test("#520 replace posts fresh=true with the same origin", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "update", { fresh: true });
  await waitFor(() =>
    expect(lastPost(fetchMock)).toEqual({ action: "update", target: "pu-mop-1",
      params: { fresh: true } }));
});

test("#520 remove posts fresh=true with the new origin", async () => {
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

test("#520 unknown is named unknown, never failed", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "update", { fresh: false });
  await waitFor(() => expect(screen.getByTestId("update-verdict"))
    .toHaveTextContent(/исход неизвестен/i));
  expect(screen.getByTestId("update-verdict")).not.toHaveTextContent(/не удался/);
});

test("#520 double submit rides one POST", async () => {
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

test("#520 a guest sees no update at all", () => {
  vi.stubGlobal("fetch", vi.fn());
  const order = new Order();
  mountEngine(order, ROW, { login: null, role: null });
  expect(screen.queryByRole("button", { name: /обнов/i })).toBeNull();
});

// #487/#513: разделение дверей. Повторный вход при непоказанном ходе
// визард divert-ит на вердикт (engineState), сюда ордер не доходит;
// ордер здесь -- всегда новая команда с финального экрана визарда,
// терминальный ход не глотается. HYPOTHESIS: движковый гард «показать
// старый вердикт вместо POST» глотал бы новую команду; SOLUTION: гард
// живёт в визарде, движок постит каждый ордер, дедуп живого хода --
// useAdminOp.
test("#520 an order after a terminal verdict is a new command and posts", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("failed", "review refusal")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "update", { fresh: false });
  await waitFor(() =>
    expect(screen.getByTestId("update-verdict")).toHaveTextContent(/review refusal/i));
  // вердикт закрыли; новая команда с финального экрана -- второй POST
  fireEvent.click(screen.getByRole("button", { name: "закрыть" }));
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  send(view, order, "update", { fresh: false });
  await waitFor(() =>
    expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
      .toHaveLength(2));
});
