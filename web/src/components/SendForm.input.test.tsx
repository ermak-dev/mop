// HYPOTHESIS: Enter не отправляет; accepted и метка занимают место,
// а ошибки без id теряются при дедупликации балунов по id.
// SOLUTION: клавиши используют общий send, промежуточные исходы молчат,
// каждая завершённая попытка с ошибкой даёт один балун, в том числе без id.
// RESULT: проверены клавиши, IME, pending, повторная попытка и отсутствие inline-вердикта.
// STATUS: FIXED — see #492
// #627: тот же контракт через общий store -- POST /api/v1/messages, исход из
// подтверждённой Operation после WS-инвалидации, отказ -- DRF error envelope.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { SendForm } from "./SendForm";
import { renderUi } from "../test-utils";
import { ADMIN, ID, deferred, networkFixture, reply } from "../network-test-utils";

const notice = vi.hoisted(() => ({ show: vi.fn() }));
vi.mock("@mantine/notifications", () => ({ notifications: { show: notice.show } }));
let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); notice.show.mockClear(); });
afterEach(() => { net.stop(); vi.unstubAllGlobals(); });

const open = async () => { await net.start(); return renderUi(<SendForm session={ADMIN} target={ID} />); };
const input = () => screen.getByRole("textbox", { name: "Сообщение" });
const type = (message = "hello") => fireEvent.change(input(), { target: { value: message } });
const finish = async (state: string, reason = "") => {
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => String(url).startsWith("/api/v1/operations")
    ? Promise.resolve(reply(net.operation(state, "3", reason))) : base(url, init));
  await net.invalidateOperation();
};
const body = (index = 0) => JSON.parse(String(net.posts()[index][1]?.body));

test.each([false, true])("#492 Enter sends once with ctrl=%s", async (ctrlKey) => {
  await open(); type("  hello\nworld  ");
  expect(fireEvent.keyDown(input(), { key: "Enter", code: "Enter", ctrlKey })).toBe(false);
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  expect(net.posts()[0][0]).toBe("/api/v1/messages");
  expect(body().params).toEqual({ message: "hello\nworld" });
  expect(body().target_id).toBe(ID);
  expect(input()).toHaveValue("");
  await finish("succeeded");
  type("next");
  await waitFor(() => expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled());
  expect(notice.show).not.toHaveBeenCalled();
});

test("#492 the field has an accessible name but no visible label", async () => {
  await open();
  expect(input()).toHaveAttribute("placeholder", "Сообщение папету");
  expect(screen.queryByText("Сообщение", { exact: true })).toBeNull();
});

test.each([false, true])("#492 Shift+Enter leaves native newline handling with ctrl=%s", async (ctrlKey) => {
  await open(); type();
  expect(fireEvent.keyDown(input(), { key: "Enter", shiftKey: true, ctrlKey })).toBe(true);
  expect(net.posts()).toHaveLength(0);
});

test.each([{ isComposing: true }, { keyCode: 229 }, { repeat: true }])(
  "#492 composition and held keys cannot send (%j)", async (event) => {
    await open(); type();
    fireEvent.keyDown(input(), { key: "Enter", ...event });
    expect(net.posts()).toHaveLength(0);
    expect(input()).toHaveValue("hello");
  });

test.each([false, true])("#492 empty Enter posts nothing with ctrl=%s", async (ctrlKey) => {
  await open(); type("  \n ");
  fireEvent.keyDown(input(), { key: "Enter", ctrlKey });
  expect(net.posts()).toHaveLength(0);
});

test("#492 accepted is silent, remains in-flight, and a terminal result unlocks the next send", async () => {
  await open(); type();
  fireEvent.keyDown(input(), { key: "Enter" });
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  type("next message");
  await waitFor(() => expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled());
  expect(screen.queryByText("Принято")).toBeNull();
  expect(screen.queryByTestId("send-verdict")).toBeNull();
  expect(notice.show).not.toHaveBeenCalled();
  fireEvent.keyDown(input(), { key: "Enter", ctrlKey: true });
  expect(net.posts()).toHaveLength(1);
  await finish("succeeded");
  await waitFor(() => expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled());
  fireEvent.keyDown(input(), { key: "Enter", ctrlKey: true });
  await waitFor(() => expect(net.posts()).toHaveLength(2));
});

test.each(["failed", "refused", "partial", "unknown"])("#492 %s produces one error balloon, not an inline verdict", async (status) => {
  await open(); type(); fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  await finish(status, "safe reason");
  await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(1));
  expect(notice.show.mock.calls[0][0]).toEqual(expect.objectContaining({
    title: expect.stringContaining(ID), message: expect.stringContaining("safe reason"),
  }));
  expect(screen.queryByText(/safe reason/)).toBeNull();
  expect(screen.queryByTestId("send-verdict")).toBeNull();
  type("next draft");
  expect(notice.show).toHaveBeenCalledTimes(1);
});

test("#492 consecutive submission errors without an id each show one balloon", async () => {
  await open();
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => init?.method === "POST"
    ? Promise.resolve(reply({ schema_version: 1, error: { code: "access_denied", message: "admin only",
      retryable: false } }, 403)) : base(url, init));
  for (let attempt = 1; attempt <= 2; attempt++) {
    type(`message ${attempt}`); fireEvent.keyDown(input(), { key: "Enter" });
    await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(attempt));
    expect(notice.show.mock.calls[attempt - 1][0].message).toContain("admin only");
  }
  expect(net.posts()).toHaveLength(2);
});

test("#492 lost submission response is unknown rather than delivered", async () => {
  await open();
  const base = net.http.getMockImplementation()!, lost = deferred();
  net.http.mockImplementation((url, init) => {
    if (init?.method === "POST") { net.capture(init); return Promise.reject(new Error("offline")); }
    // Ответ потерян: поиск по request_id ещё не видит COMMIT -- 404, не отказ.
    if (String(url).startsWith("/api/v1/operations")) return Promise.resolve(reply({ schema_version: 1,
      error: { code: "not_found", message: "resource not found", retryable: false } }, 404));
    return base(url, init);
  });
  void lost;
  type(); fireEvent.keyDown(input(), { key: "Enter" });
  await act(async () => { await Promise.resolve(); });
  await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(1));
  expect(notice.show.mock.calls[0][0]).toEqual(expect.objectContaining({
    color: "yellow", message: expect.stringContaining("Исход неизвестен"),
  }));
  expect(notice.show.mock.calls[0][0].message).toContain("первый ответ потерян");
});
