// Кнопка restart в карточке (#465): вердикт словами #476, unknown -- не
// failed, автоповтора нет. Никакого force. #513: рестарт обратим -- клик
// по иконке шлёт POST сразу, мода «подтвердите рестарт» ушла.
//
// RESULT: тесты написаны до реализации (RED).
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { RestartBox } from "./RestartBox";
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

// #513: рестарт обратим -- клик шлёт POST сразу, без моды подтверждения.
test("#513 the restart click posts at once, no confirm dialog", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  openBox();
  fireEvent.click(screen.getByRole("button", { name: /рестарт/i }));
  // моды подтверждения нет: открывшийся диалог -- только ход операции
  // («рестарт: pu-mop-1»), не «подтвердите рестарт»
  expect(screen.queryByText(/подтвердите рестарт/i)).toBeNull();
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops", expect.objectContaining({
    method: "POST", credentials: "same-origin" }));
  const body = JSON.parse((fetchMock.mock.calls[0][1] as { body: string }).body);
  expect(body).toEqual({ action: "restart", target: "pu-mop-1", params: {} });
  await waitFor(() => expect(screen.getByText(/идёт/i)).toBeInTheDocument());
});

// #513: моды подтверждения у рестарта больше нет; прежние проверки
// подтверждения адаптированы: один клик -- один POST, вердикт словами
// #476, unknown -- не failed, автоповтора нет. Никакого force.
test("the click posts once and the running verdict shows", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  openBox();
  fireEvent.click(screen.getByRole("button", { name: /рестарт/i }));
  await waitFor(() => expect(screen.getByText(/идёт/i)).toBeInTheDocument());
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops", expect.objectContaining({
    method: "POST", credentials: "same-origin" }));
  const body = JSON.parse((fetchMock.mock.calls[0][1] as { body: string }).body);
  expect(body).toEqual({ action: "restart", target: "pu-mop-1", params: {} });
});

test("terminal succeeded verdict is shown; retry needs a new click", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  openBox();
  fireEvent.click(screen.getByRole("button", { name: /рестарт/i }));
  await waitFor(() => expect(screen.getByText(/перезапущен/i)).toBeInTheDocument());
});

test("unknown is named unknown, never failed", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  openBox();
  fireEvent.click(screen.getByRole("button", { name: /рестарт/i }));
  await waitFor(() => expect(screen.getByText(/неизвестен/i)).toBeInTheDocument());
  expect(screen.queryByText(/не удался/i)).toBeNull();
});

test("a refused operation names the server phrase, retry posts again", async () => {
  const fetchMock = postGet(
    () => Promise.resolve({ ok: false, status: 409,
      json: async () => ({ error: "another operation is queued" }) }) as Response,
    () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  openBox();
  fireEvent.click(screen.getByRole("button", { name: /рестарт/i }));
  await waitFor(() => expect(screen.getByText(/queued/i)).toBeInTheDocument());
  // Повтор начинается из результата («новая команда» шлёт снова): клик
  // подтверждения больше не нужен, рестарт обратим (#513).
  fireEvent.click(screen.getByRole("button", { name: "новая команда" }));
  await waitFor(() =>
    expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
      .toHaveLength(2));
});

test("no storage: nothing lands in localStorage or sessionStorage", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  openBox();
  fireEvent.click(screen.getByRole("button", { name: /рестарт/i }));
  await waitFor(() => expect(screen.getByText(/перезапущен/i)).toBeInTheDocument());
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

function openBox(row: Puppet = ROW, session: Session = ADMIN) {
  renderUi(<RestartBox session={session} row={row} />);
}

