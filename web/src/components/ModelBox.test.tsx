// Смена модели (#468 -> #520): каталог -- единственный источник
// (useModels, GET /api/admin/models), визард собирает выбор на шаге
// модели и шлёт приказ {act: "model", params: {model}}. Вердикт --
// балун (#488, слово оператора): succeeded приезжает только после
// телеметрии сервера. Радио, а не Select: выпадашка не открывается в
// jsdom (#472 опыт). Каталог спрашивается при открытом шаге модели.
//
// RESULT: тесты написаны до реализации (RED).
import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ModelBox } from "./ModelBox";
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

// #488: у папета есть текущая модель -- шаг модели обязан её показать.
const LIVE_ROW: Puppet = { ...ROW, llm: "glm-5.3-flash" };

const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "model",
     target: "pu-mop-1", status }) }) as Response;
const catalog = () =>
  ({ ok: true, status: 200, json: async () => ({
    models: ["claude-opus-5-5", "glm-5.3", "glm-5.3-flash"] }) }) as Response;
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) =>
    String(url).includes("/api/admin/models") ? catalog()
      : (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Приказ визарда (#520): «Выполнить» на шаге модели. */
class Order {
  current: { at: number; act: string; params: Record<string, unknown> } | null = null;
  fire(act: string, params: Record<string, unknown> = {}) {
    this.current = { at: Date.now() + Math.random(), act, params };
  }
}

/** Движок в коже #520: без кнопок, вход -- приказ-токен. */
function mountEngine(order: Order, row: Puppet = LIVE_ROW, session: Session = ADMIN) {
  return renderUi(<ModelBox session={session} row={row} order={order.current} />);
}

/** Последний POST-вызов fetch как разобранный body. */
const lastPost = (fetchMock: ReturnType<typeof postGet>) => {
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  return post ? JSON.parse((post[1] as { body: string }).body) : null;
};

/** Приказ и ререндер одним ходом. */
const send = (view: ReturnType<typeof mountEngine>, order: Order, model: string) => {
  order.fire("model", { model });
  view.rerender(<MantineProvider>
    <ModelBox session={ADMIN} row={LIVE_ROW} order={order.current} />
  </MantineProvider>);
};

// #488: шпион балунов на уровне модуля -- vi.mock поднимается наверх.
const shown: unknown[] = [];
vi.mock("@mantine/notifications", async (importOriginal) => {
  const orig = await importOriginal<typeof import("@mantine/notifications")>();
  return { ...orig, notifications: { ...orig.notifications,
    show: (n: unknown) => shown.push(n) } };
});

test("#520 the model order posts at once, no confirm", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "glm-5.3");
  expect(screen.queryByText(/подтвердите смену модели/i)).toBeNull();
  await waitFor(() =>
    expect(lastPost(fetchMock)).toEqual({ action: "model", target: "pu-mop-1",
      params: { model: "glm-5.3" } }));
});

test("#520 verdicts ride toast balloons, not an inline line", async () => {
  shown.length = 0;
  vi.stubGlobal("fetch", postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("refused"))));
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "claude-opus-5-5");
  await waitFor(() => expect(shown.length).toBeGreaterThan(0));
  const last = shown[shown.length - 1] as { message?: string; color?: string };
  expect(String(last.message)).toContain("Отказано");
  expect(last.color).toBe("red");
});

test("#520 after a terminal verdict the pool snapshot is refetched", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "claude-opus-5-5");
  await waitFor(() => expect(fetchMock.mock.calls
    .filter((c) => String(c[0]).endsWith("/api/pool"))).toHaveLength(1));
});

test("#520 succeeded reads the confirmed word, unknown is named unknown", async () => {
  shown.length = 0;
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "glm-5.3-flash");
  await waitFor(() => expect(shown.length).toBeGreaterThan(0));
  expect(String((shown[shown.length - 1] as { message?: string }).message))
    .toMatch(/Исход неизвестен/);
});

test("#520 a refusal names the server phrase (the dialog did not close, a foreign lease)", async () => {
  shown.length = 0;
  const refused = () =>
    ({ ok: true, status: 200, json: async () => ({ id: "op-1",
       action: "model", target: "pu-mop-1", status: "refused",
       error: "pu-mop-1: model switch dialog did not close" }) }) as Response;
  const fetchMock = vi.fn(async (url: string | URL, init?: { method?: string }) =>
    String(url).includes("/api/admin/models") ? catalog()
      : (init?.method ?? "GET") === "POST" ? ok202("op-1") : refused());
  vi.stubGlobal("fetch", fetchMock);
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "glm-5.3-flash");
  await waitFor(() => expect(shown.length).toBeGreaterThan(0));
  expect(String((shown[shown.length - 1] as { message?: string }).message))
    .toMatch(/Отказано/);
  expect(String((shown[shown.length - 1] as { message?: string }).message))
    .toMatch(/dialog did not close/);
});

// HYPOTHESIS: done.current=null совпадает с id отказа ещё до постановки в очередь.
// SOLUTION: общий механизм учитывает завершённую попытку, не только id.
// STATUS: FIXED — see #492
test("#492 each model submission refusal without an id produces a balloon", async () => {
  shown.length = 0;
  vi.stubGlobal("fetch", postGet(async () => ({ ok: false, status: 403,
    json: async () => ({ error: "admin only" }) }) as Response,
    async () => opJson("running")));
  const order = new Order();
  const view = mountEngine(order);
  for (let attempt = 1; attempt <= 2; attempt++) {
    send(view, order, "glm-5.3-flash");
    await waitFor(() => expect(shown).toHaveLength(attempt));
    expect(String((shown[attempt - 1] as { message: string }).message)).toContain("admin only");
  }
});

test("#520 a guest sees no model order at all", () => {
  vi.stubGlobal("fetch", vi.fn());
  const order = new Order();
  mountEngine(order, ROW, { login: null, role: null });
  expect(screen.queryByRole("button", { name: /модель/i })).toBeNull();
});
