// HYPOTHESIS: Enter не отправляет; accepted и метка занимают место,
// а ошибки без id теряются при дедупликации балунов по id.
// SOLUTION: клавиши используют общий send, промежуточные исходы молчат,
// каждая завершённая попытка с ошибкой даёт один балун, в том числе без id.
// RESULT: проверены клавиши, IME, pending, повторная попытка и отсутствие inline-вердикта.
// STATUS: FIXED — see #492
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { SendForm } from "./SendForm";
import { renderUi } from "../test-utils";

const notice = vi.hoisted(() => ({ show: vi.fn() }));
vi.mock("@mantine/notifications", () => ({ notifications: { show: notice.show } }));
const ADMIN = { login: "admin", role: "admin" as const };
const response = (body: unknown, status = 200) => ({ ok: status < 400, status,
  json: async () => body }) as Response;
const accepted = () => response({ id: "op-1", status: "accepted" }, 202);
const open = () => renderUi(<SendForm session={ADMIN} target="pu-mop-1" />);
const input = () => screen.getByRole("textbox", { name: "Сообщение" });
const type = (message = "hello") => fireEvent.change(input(), { target: { value: message } });
const route = (get: () => Promise<Response>) => vi.fn(async (_url: string, init?: RequestInit) =>
  init?.method === "POST" ? accepted() : get());
const posts = (mock: ReturnType<typeof route>) => mock.mock.calls.filter((c) => c[1]?.method === "POST");

beforeEach(() => notice.show.mockClear());
afterEach(() => vi.unstubAllGlobals());

test.each([false, true])("#492 Enter sends once with ctrl=%s", async (ctrlKey) => {
  const mock = route(async () => response({ status: "succeeded" })); vi.stubGlobal("fetch", mock);
  open(); type("  hello\nworld  ");
  expect(fireEvent.keyDown(input(), { key: "Enter", code: "Enter", ctrlKey })).toBe(false);
  await waitFor(() => expect(posts(mock)).toHaveLength(1));
  expect(JSON.parse(posts(mock)[0][1]!.body as string)).toEqual({
    action: "send", target: "pu-mop-1", params: { message: "hello\nworld" },
  });
  expect(input()).toHaveValue("");
  type("next");
  await waitFor(() => expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled());
  expect(notice.show).not.toHaveBeenCalled();
});

test("#492 the field has an accessible name but no visible label", () => {
  open();
  expect(input()).toHaveAttribute("placeholder", "Сообщение папету");
  expect(screen.queryByText("Сообщение", { exact: true })).toBeNull();
});

test.each([false, true])("#492 Shift+Enter leaves native newline handling with ctrl=%s", (ctrlKey) => {
  const mock = vi.fn(); vi.stubGlobal("fetch", mock); open(); type();
  expect(fireEvent.keyDown(input(), { key: "Enter", shiftKey: true, ctrlKey })).toBe(true);
  expect(mock).not.toHaveBeenCalled();
});

test.each([{ isComposing: true }, { keyCode: 229 }, { repeat: true }])(
  "#492 composition and held keys cannot send (%j)", (event) => {
    const mock = vi.fn(); vi.stubGlobal("fetch", mock); open(); type();
    fireEvent.keyDown(input(), { key: "Enter", ...event });
    expect(mock).not.toHaveBeenCalled();
    expect(input()).toHaveValue("hello");
  });

test.each([false, true])("#492 empty Enter posts nothing with ctrl=%s", (ctrlKey) => {
  const mock = vi.fn(); vi.stubGlobal("fetch", mock); open(); type("  \n ");
  fireEvent.keyDown(input(), { key: "Enter", ctrlKey });
  expect(mock).not.toHaveBeenCalled();
});

test("#492 accepted is silent, remains in-flight, and a terminal result unlocks the next send", async () => {
  let finish!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  const mock = route(() => pending); vi.stubGlobal("fetch", mock); open(); type();
  fireEvent.keyDown(input(), { key: "Enter" });
  await waitFor(() => expect(mock.mock.calls.some((c) => c[0].endsWith("/op-1"))).toBe(true));
  type("next message");
  expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  expect(screen.queryByText("Принято")).toBeNull();
  expect(screen.queryByTestId("send-verdict")).toBeNull();
  expect(notice.show).not.toHaveBeenCalled();
  fireEvent.keyDown(input(), { key: "Enter", ctrlKey: true });
  expect(posts(mock)).toHaveLength(1);
  await act(async () => { finish(response({ status: "succeeded" })); });
  expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled();
  fireEvent.keyDown(input(), { key: "Enter", ctrlKey: true });
  await waitFor(() => expect(posts(mock)).toHaveLength(2));
});

test.each(["failed", "refused", "partial", "unknown"])("#492 %s produces one error balloon, not an inline verdict", async (status) => {
  const mock = route(async () => response({ status, error: "safe reason" }));
  vi.stubGlobal("fetch", mock); open(); type(); fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
  await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(1));
  expect(notice.show.mock.calls[0][0]).toEqual(expect.objectContaining({
    title: expect.stringContaining("pu-mop-1"), message: expect.stringContaining("safe reason"),
  }));
  expect(screen.queryByText(/safe reason/)).toBeNull();
  expect(screen.queryByTestId("send-verdict")).toBeNull();
  type("next draft");
  expect(notice.show).toHaveBeenCalledTimes(1);
});

test("#492 consecutive submission errors without an id each show one balloon", async () => {
  const mock = vi.fn(async () => response({ error: "admin only" }, 403));
  vi.stubGlobal("fetch", mock); open();
  for (let attempt = 1; attempt <= 2; attempt++) {
    type(`message ${attempt}`); fireEvent.keyDown(input(), { key: "Enter" });
    await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(attempt));
    expect(notice.show.mock.calls[attempt - 1][0].message).toContain("admin only");
  }
  expect(mock).toHaveBeenCalledTimes(2);
});

test("#492 lost submission response is unknown rather than delivered", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  open(); type(); fireEvent.keyDown(input(), { key: "Enter" });
  await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(1));
  expect(notice.show.mock.calls[0][0]).toEqual(expect.objectContaining({
    color: "yellow", message: expect.stringContaining("Исход неизвестен"),
  }));
  expect(notice.show.mock.calls[0][0].message).toContain("первый ответ потерян");
});
