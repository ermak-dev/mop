// Смена модели (#468 -> #520): визард собирает выбор на шаге модели и
// шлёт приказ {act: "model", params: {model}}. Вердикт -- балун (#488,
// слово оператора). Радио, а не Select: выпадашка не открывается в
// jsdom (#472 опыт).
// #627: общий store контроллера. Смену модели контроллер ещё не
// принимает: приказ отказывается до POST, балун называет причину.
// HYPOTHESIS: после вердикта движок дёргает старый GET /api/pool, которого у
// контроллера нет; снимок теперь обновляет WS-инвалидация общего store.
// SOLUTION: onSettled с GET /api/pool убран.
import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ModelBox } from "./ModelBox";
import { MantineProvider } from "@mantine/core";
import { renderUi } from "../test-utils";
import { ADMIN, ID, networkFixture } from "../network-test-utils";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ROW: Puppet = {
  name: ID, node: "node", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "glm-5.3-flash", origin: "git@h:g/mop.git", disk_kb: null,
};

let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); shown.length = 0; });
afterEach(() => { net.stop(); cleanup(); vi.unstubAllGlobals(); });

/** Приказ визарда (#520): «Выполнить» на шаге модели. */
class Order {
  current: { at: number; act: string; params: Record<string, unknown> } | null = null;
  fire(act: string, params: Record<string, unknown> = {}) {
    this.current = { at: Date.now() + Math.random(), act, params };
  }
}

/** Движок в коже #520: без кнопок, вход -- приказ-токен. */
function mountEngine(order: Order, session: Session = ADMIN) {
  return renderUi(<ModelBox session={session} row={ROW} order={order.current} />);
}

/** Приказ и ререндер одним ходом. */
const send = (view: ReturnType<typeof mountEngine>, order: Order, model: string) => {
  order.fire("model", { model });
  view.rerender(<MantineProvider>
    <ModelBox session={ADMIN} row={ROW} order={order.current} />
  </MantineProvider>);
};

// #488: шпион балунов на уровне модуля -- vi.mock поднимается наверх.
const shown: unknown[] = [];
vi.mock("@mantine/notifications", async (importOriginal) => {
  const orig = await importOriginal<typeof import("@mantine/notifications")>();
  return { ...orig, notifications: { ...orig.notifications,
    show: (n: unknown) => shown.push(n) } };
});
const last = () => shown[shown.length - 1] as { message?: string; color?: string };

test("#627 the model order is refused before any POST and the balloon names why", async () => {
  await net.start();
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "glm-5.3");
  expect(screen.queryByText(/подтвердите смену модели/i)).toBeNull();
  await waitFor(() => expect(shown).toHaveLength(1));
  expect(String(last().message)).toContain("Отказано");
  expect(String(last().message)).toContain("not implemented by the controller");
  expect(last().color).toBe("red");
  expect(net.posts()).toHaveLength(0);
});

test("#627 a settled verdict never reaches for the legacy pool snapshot", async () => {
  await net.start();
  const order = new Order();
  const view = mountEngine(order);
  send(view, order, "claude-opus-5-5");
  await waitFor(() => expect(shown).toHaveLength(1));
  await new Promise((r) => setTimeout(r, 0));
  expect(net.http.mock.calls.filter(([url]) => String(url).includes("/api/pool"))).toHaveLength(0);
});

// HYPOTHESIS: done.current=null совпадает с id отказа ещё до постановки в очередь.
// SOLUTION: общий механизм учитывает завершённую попытку, не только id.
// STATUS: FIXED — see #492
test("#492 each model refusal without an id produces a balloon", async () => {
  await net.start();
  const order = new Order();
  const view = mountEngine(order);
  for (let attempt = 1; attempt <= 2; attempt++) {
    send(view, order, "glm-5.3-flash");
    await waitFor(() => expect(shown).toHaveLength(attempt));
    expect(String((shown[attempt - 1] as { message: string }).message)).toContain("not implemented");
  }
  expect(net.posts()).toHaveLength(0);
});

test("#520 a guest sees no model order at all", () => {
  vi.stubGlobal("fetch", vi.fn());
  mountEngine(new Order(), { login: null, role: null });
  expect(screen.queryByRole("button", { name: /модель/i })).toBeNull();
  expect(vi.mocked(fetch)).not.toHaveBeenCalled();
});
