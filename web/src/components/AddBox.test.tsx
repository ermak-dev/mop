// Форма add (#472): админ выбирает заведённый проект из реестра сервера
// (useProjects -- один источник, имя и origin с сервера), ветка опциональна,
// имя папета приходит ОТ СЕРВЕРА -- страница его не выдумывает. POST
// /api/admin/ops {action: "add", params: {origin, branch?}} -- без target.
// Результат -- общий ход useAdminOp: 202 -> id -> «принято, исход
// неизвестен» и опрос из памяти страницы; перезагрузка начинает заново --
// id хранить негде (storage запрещён #463), это названо.
//
// RESULT: тесты написаны до реализации (RED).
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AddBox } from "./AddBox";
import { renderUi } from "../test-utils";
import type { Session } from "../useSession";

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

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

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
  // 202 принят, опрос по id из памяти: первый же GET даёт правду
  await waitFor(() => expect(screen.getByTestId("add-verdict")).toHaveTextContent(/Идёт/));
  expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/api/admin/ops/op-1"))).toBe(true);
});

test("a dropped answer (unknown) is named unknown, never failed", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  await waitFor(() => expect(screen.getByTestId("add-verdict"))
    .toHaveTextContent(/Исход неизвестен/));
  // unknown -- не failed: слова «не удался» нет
  expect(screen.getByTestId("add-verdict")).not.toHaveTextContent(/не удался/);
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

test("terminal verdict replaces the unknown phrase; storage stays empty", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  await waitFor(() => expect(screen.getByTestId("add-verdict")).toHaveTextContent(/заведён/i));
  expect(window.localStorage.length).toBe(0);
  expect(window.sessionStorage.length).toBe(0);
});

test("the form does not close itself on submit: the verdict is visible", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<AddBox session={ADMIN} />);
  await openForm();
  fireEvent.click(await screen.findByRole("radio", { name: /mop/i }));
  fireEvent.click(screen.getByRole("button", { name: /^завести$/i }));
  await waitFor(() => expect(screen.getByTestId("add-verdict")).toBeInTheDocument());
  // выбор проекта ещё на экране -- ход операции виден целиком
  expect(screen.getByTestId("add-project")).toBeInTheDocument();
});
