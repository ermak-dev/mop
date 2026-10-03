// Хук реестра проектов для формы add (#472): GET /api/admin/projects --
// admin-only no-store, поэтому список -- только у админа и только по явной
// необходимости. Один источник: {name, origin} приходит с сервера, страница
// ничего не придумывает второй раз (проект из origin -- project_of #154).
//
// HYPOTHESIS: у формы add нет своего множества проектов; отказы -- те же
// слова, что у карточки (usePuppetCard.refuseText), второй словарь не нужен.
// RESULT: тесты написаны до реализации (RED).
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useProjects } from "./useProjects";
import type { Session } from "./useSession";

const ADMIN: Session = { login: "anton", role: "admin" };
const GUEST: Session = { login: null, role: null };

const listing = () =>
  ({ ok: true, status: 200, json: async () => ({
    projects: [
      { name: "mop", origin: "git@h:g/mop.git" },
      { name: "web", origin: "https://x/y/web.git" },
    ] }) }) as Response;

afterEach(() => vi.unstubAllGlobals());

test("the admin gets the registry: name and origin come from the server", async () => {
  const fetchMock = vi.fn(async () => listing());
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useProjects(ADMIN, true));
  await waitFor(() => expect(view.result.current.projects).toHaveLength(2));
  expect(view.result.current.projects[0]).toEqual(
    { name: "mop", origin: "git@h:g/mop.git" });
  expect(view.result.current.error).toBeNull();
  // GET /api/admin/projects, не выдуманный URL
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/projects",
    expect.objectContaining({ credentials: "same-origin" }));
});

test("no request until asked: the list is not polled on mount", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  renderHook(() => useProjects(ADMIN, false));
  await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  expect(fetchMock).not.toHaveBeenCalled();
});

test("asking loads once: repeats are served from the page memory", async () => {
  const fetchMock = vi.fn(async () => listing());
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useProjects(ADMIN, true));
  await waitFor(() => expect(view.result.current.projects).toHaveLength(2));
  const after = fetchMock.mock.calls.length;
  // повторный ask -- без нового запроса: реестр не меняется каждые полсекунды
  act(() => { view.result.current.ask(); });
  await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  expect(fetchMock.mock.calls.length).toBe(after);
});

test("a guest asks nothing and holds no list", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useProjects(GUEST, true));
  await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  expect(fetchMock).not.toHaveBeenCalled();
  expect(view.result.current.projects).toEqual([]);
  // не-admin хуком не обслуживается: формы у него нет -- она проверяет
  // роль сама и отказывается показывать, не хук
  expect(view.result.current.projects).toEqual([]);
});

test("role loss wipes the list in the same render", async () => {
  const fetchMock = vi.fn(async () => listing());
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ s }: { s: Session }) => useProjects(s, true),
                          { initialProps: { s: ADMIN } });
  await waitFor(() => expect(view.result.current.projects).toHaveLength(2));
  view.rerender({ s: GUEST });
  // гостю список не остаётся -- тот же рендер, без эффекта после paint
  expect(view.result.current.projects).toEqual([]);
});

test("a refusal names the same refusal words as the card", async () => {
  const fetchMock = vi.fn(async () =>
    ({ ok: false, status: 403, json: async () => ({}) }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useProjects(ADMIN, true));
  await waitFor(() => expect(view.result.current.error).toBeTruthy());
  // слова -- общий словарь отказов, не новый
  expect(view.result.current.error).toBe("карточка недоступна: прав не хватает");
});

test("a silent panel reads as an error, not as an empty registry", async () => {
  const fetchMock = vi.fn(async () =>
    ({ ok: false, status: 500, json: async () => ({}) }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useProjects(ADMIN, true));
  await waitFor(() => expect(view.result.current.error).toBeTruthy());
  expect(view.result.current.projects).toEqual([]);
});
