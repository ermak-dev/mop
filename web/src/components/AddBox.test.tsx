// Форма add (#472): админ выбирает заведённый проект из реестра сервера
// (useProjects -- один источник, имя и origin с сервера), ветка опциональна,
// имя папета приходит ОТ СЕРВЕРА -- страница его не выдумывает. POST
// /api/admin/ops {action: "add", params: {origin, branch?}} -- без target.
// HYPOTHESIS: #605 форма сохраняет выбор и остаётся открытой после 202,
// а вердикт вынесен за Modal; терминал позволяет повторить add одним кликом.
// SOLUTION: подтверждённый id закрывает и очищает форму; отказ остаётся
// внутри Modal, следующий add требует нового выбора, loading явный.
// RESULT: семь проверок воспроизвели дефект до исправления; interaction-suite зелёный.
// STATUS: FIXED — see #605
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AddBox } from "./AddBox";
import { renderUi } from "../test-utils";
import type { Session } from "../useSession";
import { operations } from "../useOperationFeed";

const ADMIN: Session = { login: "anton", role: "admin" };

const REGISTRY = [
  { name: "mop", origin: "git@h:g/mop.git" },
  { name: "web", origin: "https://x/y/web.git" },
];

const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "add",
     target: null, status }) }) as Response;
/** GET по URL: реестр -- реестром, результат -- результатом. */
const route = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) => {
    const u = String(url);
    if ((init?.method ?? "GET") === "POST") return post();
    if (u.includes("/api/admin/projects")) return ({ ok: true, status: 200,
      json: async () => ({ projects: REGISTRY }) }) as Response;
    if (u.includes("/api/admin/ops")) return get();
    return ({ ok: true, status: 200, json: async () => ({}) }) as Response;
  });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); operations.resetForTest(); });

const openForm = async () => {
  fireEvent.click(screen.getByRole("button", { name: /завести папета/i }));
  await screen.findByTestId("add-project");
};

test("the add button lives on the page and opens the form for the admin", async () => {
  vi.stubGlobal("fetch", route(() => Promise.resolve(ok202("op-1")),
                               () => Promise.resolve(opJson("running"))));
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  // источник -- реестр сервера: оба проекта в выборе, origin рядом
  expect(screen.getByText("git@h:g/mop.git")).toBeInTheDocument();
  expect(screen.getByText("https://x/y/web.git")).toBeInTheDocument();
});

test("a guest and a user see no add button at all", () => {
  vi.stubGlobal("fetch", vi.fn());
  const view = renderUi(<AddBox session={{ login: null, role: null }} />);
  expect(screen.queryByRole("button", { name: /завести папета/i })).toBeNull();
  view.rerender(<><AddBox session={{ login: "olga", role: "user" }} /></>);
  // обёртки нет -- рендерим заново под провайдером
  view.unmount();
  renderUi(<AddBox session={{ login: "olga", role: "user" }} />);
  expect(screen.queryByRole("button", { name: /завести папета/i })).toBeNull();
});

test("picking a project shows its origin from the registry, no invented name", async () => {
  vi.stubGlobal("fetch", route(() => Promise.resolve(ok202("op-1")),
                               () => Promise.resolve(opJson("running"))));
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  // имя папета -- не поле формы: его говорит сервер
  expect(screen.queryByLabelText(/имя папета/i)).toBeNull();
  // ветка -- опциональна: подпись говорит это сама
  expect(screen.getByText(/Ветка \(необязательна\)/)).toBeInTheDocument();
});

test("submit posts {action add, origin, branch?} without target", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.change(screen.getByLabelText(/ветка/i), { target: { value: "master" } });
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops",
    expect.objectContaining({ method: "POST" })));
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  expect(JSON.parse((post?.[1] as { body: string }).body))
    .toEqual({ action: "add", params: { origin: "git@h:g/mop.git", branch: "master" } });
});

test("submit without a project posts nothing", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(0);
});

test("after 202 the poll reads the outcome by id from the page memory", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  await waitFor(() => expect(fetchMock.mock.calls.some((c) =>
    String(c[0]).includes("/api/admin/ops/op-1"))).toBe(true));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.queryByTestId("add-verdict")).toBeNull();
  expect(screen.getByRole("button", { name: /завести папета/i })).toBeDisabled();
});

test("a dropped first answer stays unknown inside the form, never failed", async () => {
  const fetchMock = route(() => Promise.reject(new Error("network")),
                          () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  await waitFor(() => expect(screen.getByTestId("add-verdict"))
    .toHaveTextContent(/Исход неизвестен/));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByTestId("add-verdict")).toHaveTextContent(/первый ответ потерян/);
  expect(within(dialog).getByTestId("add-verdict")).not.toHaveTextContent(/не удался/);
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("double submit rides one POST", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = route(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  const btn = screen.getByRole("button", { name: /^завести$/i });
  fireEvent.click(btn);
  fireEvent.click(btn);
  await act(async () => { release?.(ok202("op-9")); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("a fast terminal result closes the form without a dashboard verdict; storage stays empty", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await waitFor(() => expect(screen.getByRole("button", { name: /завести папета/i })).toBeEnabled());
  expect(screen.queryByTestId("add-verdict")).toBeNull();
  expect(screen.queryByText(/заведён/i)).toBeNull();
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

test("accepted add closes before placement finishes and requires a new choice after completion", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.change(screen.getByLabelText(/ветка/i), { target: { value: "swarm" } });
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.queryByTestId("add-verdict")).toBeNull();
  expect(screen.getByRole("button", { name: /завести папета/i })).toBeDisabled();
  await act(async () => { operations.frame({ id: "op-1", action: "add", target: null,
                                            status: "succeeded" }); });
  await openForm();
  expect(screen.getAllByRole("radio").every((r) => !(r as HTMLInputElement).checked)).toBe(true);
  expect(screen.getByLabelText(/ветка/i)).toHaveValue("");
  expect(screen.getByTestId("add-submit")).toBeDisabled();
  fireEvent.click(screen.getByTestId("add-submit"));
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("a refused request keeps the selected form and its reason inside the dialog", async () => {
  const fetchMock = route(() => Promise.resolve(({ ok: false, status: 409,
    json: async () => ({ error: "project limit reached" }) }) as Response),
    () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.click(screen.getByTestId("add-submit"));
  const reason = await screen.findByText("project limit reached");
  expect(screen.getByRole("dialog")).toContainElement(reason);
  expect(within(screen.getByRole("dialog")).getByTestId("add-verdict")).toHaveTextContent(/Отказано/);
  expect(screen.getByRole("radio", { name: /mop/i })).toBeChecked();
});

test("an empty loaded registry is not a perpetual loading state", async () => {
  let release: ((r: Response) => void) | null = null;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { release = resolve; })));
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  expect(screen.getByText(/реестр грузится/i)).toBeInTheDocument();
  await act(async () => { release?.(({ ok: true, status: 200,
    json: async () => ({ projects: [] }) }) as Response); });
  expect(screen.queryByText(/реестр грузится/i)).toBeNull();
  expect(screen.getByText(/нет зарегистрированных проектов/i)).toBeInTheDocument();
  expect(screen.getByTestId("add-submit")).toBeDisabled();
});

test("acceptance closes the form even while the initial result GET has no reply", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => new Promise<Response>(() => {}));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.click(screen.getByTestId("add-submit"));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.queryByTestId("add-verdict")).toBeNull();
});
