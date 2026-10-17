// #664: операции живут в заголовке панели, не в строке таблицы.
// Удаление цели/смена авторизации уничтожают открытую операцию;
// обычное переключение панели не должно терять поздний вердикт.
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ID, deferred, networkFixture, reply } from '../network-test-utils';
let net: ReturnType<typeof networkFixture>;
beforeEach(async () => { net = networkFixture(); await net.start(ADMIN); });
import { MantineProvider } from "@mantine/core";
import { PuppetActions } from "./PuppetActions";
import { PuppetCard } from "./PuppetCard";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";

const ADMIN = { login: '42', role: 'user' as const, authenticated: true };
const CARD: Puppet = {
  name: ID, node: "hyper", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};

afterEach(() => { cleanup(); net.stop(); vi.unstubAllGlobals(); });

test("#513 the restart click posts from the final screen", async () => {
  const fetchMock = net.http;
  const before = fetchMock.mock.calls.length;
  renderUi(<PuppetActions row={CARD} session={ADMIN} />);
  // вход #522: строка -> визард правки -> финальный экран рестарта ->
  // «Завершить» = согласие (единое правило подтверждения)
  fireEvent.click(screen.getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "Рестарт" }));
  // до согласия POST нет: финальный экран сам и есть подтверждение
  expect(win.textContent).toMatch(/Контекст диалога с Claude теряется/);
  expect(fetchMock.mock.calls).toHaveLength(before);
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(`/api/v1/puppets/${ID}/commands/restart`, expect.objectContaining({
    method: "POST", credentials: "same-origin" })));
});

test.each(["target", "role", "login"])("stale restart op after %s change rides nowhere", async (change) => {
  const slow = deferred(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => { if (init?.method === 'POST') { net.capture(init); return slow.promise; } return base(url, init); });
  const view = renderUi(<PuppetCard rows={[CARD]} target={CARD.name} session={ADMIN} onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "Рестарт" }));
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  view.rerender(<MantineProvider><PuppetCard rows={change === "target" ? [] : [CARD]}
    target={CARD.name} onClose={() => {}}
    session={change === "role" ? { login: null, role: null }
      : change === "login" ? { ...ADMIN, login: "other" } : ADMIN} /></MantineProvider>);
  // смена источника стирает ход: экран хода исчез вместе с окном цели
  await waitFor(() => expect(screen.queryByTestId("control-window")).toBeNull());
  await act(async () => slow.resolve(reply(net.operation('accepted', '1'), 202)));
  expect(screen.queryByTestId('control-window')).toBeNull();
  expect(net.posts()).toHaveLength(1);
});

// #627: режимы обновления (#527) проверяет визард в изоляции; через строку
// пункт виден, но закрыт, пока контроллер не принимает обновление.
test("#627 the row offers update closed with the reason until the controller admits it", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PuppetActions row={CARD} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "Правка" }));
  const win = screen.getByTestId("control-window");
  const update = within(win).getByRole("button", { name: /^Обновление/ });
  expect(update).toBeDisabled();
  expect(update).toHaveAccessibleName(expect.stringContaining("контроллер эту команду ещё не принимает"));
  fireEvent.click(update);
  expect(within(win).queryAllByRole("radio")).toHaveLength(0);
  expect(vi.mocked(fetch)).not.toHaveBeenCalled();
});

test("a guest never sees restart or update", () => {
  renderUi(<PuppetActions row={CARD} session={{ login: null, role: null }} />);
  expect(screen.queryByRole("button", { name: "Рестарт" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Правка" })).toBeNull();
});
