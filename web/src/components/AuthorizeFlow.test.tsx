// Поток входа (#300): вкладка открывается ДО запроса (блокировщик
// всплывающих окон), код уходит с именем, отказы всплывают уведомлением.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { vi } from "vitest";
import { AuthorizeFlow } from "./AuthorizeFlow";

const show = vi.fn();
vi.mock("@mantine/notifications", () => ({ notifications: { show: (...a: unknown[]) => show(...a) } }));

function reply(body: object, ok = true) {
  return Promise.resolve({ ok, json: () => Promise.resolve(body) } as Response);
}

const wrap = () => render(<MantineProvider><AuthorizeFlow name="ermak" /></MantineProvider>);

beforeEach(() => { show.mockClear(); });

test("the tab is opened before the server is asked and pointed at the url after", async () => {
  const order: string[] = [];
  const tab = { location: { href: "" }, close: vi.fn() };
  window.open = vi.fn(() => { order.push("open"); return tab as unknown as Window; });
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    order.push("fetch");
    expect(url).toBe("/api/creds/login/start");
    expect(JSON.parse(String(init?.body))).toEqual({ name: "ermak" });
    return reply({ ok: true, url: "https://claude.com/x" });
  });
  vi.stubGlobal("fetch", fetch);
  wrap();
  fireEvent.click(screen.getByTestId("auth-ermak"));
  await waitFor(() => expect(screen.getByText("открыть страницу входа")).toBeInTheDocument());
  expect(order).toEqual(["open", "fetch"]);
  expect(tab.location.href).toBe("https://claude.com/x");
  expect(screen.getByRole("link")).toHaveAttribute("href", "https://claude.com/x");
});

test("a refused start closes the tab and notifies", async () => {
  const tab = { location: { href: "" }, close: vi.fn() };
  window.open = vi.fn(() => tab as unknown as Window);
  vi.stubGlobal("fetch", vi.fn(() => reply({ error: "claude is not installed" }, false)));
  wrap();
  fireEvent.click(screen.getByTestId("auth-ermak"));
  await waitFor(() => expect(show).toHaveBeenCalled());
  expect(tab.close).toHaveBeenCalled();
  expect(show.mock.calls[0][0]).toMatchObject({ color: "red", message: "claude is not installed" });
  expect(screen.getByTestId("auth-ermak")).toBeInTheDocument();
});

test("the code is posted with the name and the outcome is announced", async () => {
  window.open = vi.fn(() => null);
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, body: JSON.parse(String(init?.body)) });
    return url.endsWith("/start") ? reply({ ok: true, url: "https://claude.com/x" }) : reply({ ok: true, owner: "anton@example.dev" });
  }));
  wrap();
  fireEvent.click(screen.getByTestId("auth-ermak"));
  const input = await screen.findByLabelText("код для ermak");
  fireEvent.change(input, { target: { value: "abc#state" } });
  fireEvent.click(screen.getByText("Отправить код"));
  await waitFor(() => expect(show).toHaveBeenCalled());
  expect(calls[1]).toEqual({ url: "/api/creds/login/code", body: { name: "ermak", code: "abc#state" } });
  expect(show.mock.calls[0][0]).toMatchObject({ color: "green", message: "вошёл как anton@example.dev" });
  expect(screen.getByTestId("auth-ermak")).toBeInTheDocument();
});
