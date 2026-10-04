// Хук каталога моделей для формы смены (#468): GET /api/admin/models --
// admin-only no-store, список -- только у админа и только по ask(). Один
// источник: id приходят с сервера (прокси -- единственный, кто знает
// каталог), страница свой список не выдумывает. Отказы -- те же слова,
// что у карточки (usePuppetCard.refuseText).
//
// RESULT: тесты написаны до реализации (RED) -- реализация зеркальна
// useProjects (#472), слова и ход те же.
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useModels } from "./useModels";
import type { Session } from "./useSession";

const ADMIN: Session = { login: "anton", role: "admin" };
const GUEST: Session = { login: null, role: null };

const listing = () =>
  ({ ok: true, status: 200, json: async () => ({
    models: ["claude-opus-5-5", "glm-5.3"] }) }) as Response;

afterEach(() => vi.unstubAllGlobals());

test("the admin gets the catalog: ids come from the server", async () => {
  const fetchMock = vi.fn(async () => listing());
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useModels(ADMIN, true));
  await waitFor(() => expect(view.result.current.models).toHaveLength(2));
  expect(view.result.current.models).toEqual(["claude-opus-5-5", "glm-5.3"]);
  expect(view.result.current.error).toBeNull();
  // GET /api/admin/models, не выдуманный URL
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/models",
    expect.objectContaining({ credentials: "same-origin" }));
});

test("no request until asked: the catalog is not polled on mount", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  renderHook(() => useModels(ADMIN, false));
  await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  expect(fetchMock).not.toHaveBeenCalled();
});

test("asking loads once: repeats are served from the page memory", async () => {
  const fetchMock = vi.fn(async () => listing());
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useModels(ADMIN, true));
  await waitFor(() => expect(view.result.current.models).toHaveLength(2));
  const n = fetchMock.mock.calls.length;
  act(() => view.result.current.ask());
  await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  expect(fetchMock.mock.calls.length).toBe(n);
});

test("a guest gets no request and no list", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useModels(GUEST, true));
  await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  expect(fetchMock).not.toHaveBeenCalled();
  expect(view.result.current.models).toEqual([]);
});

test("a refusal carries the shared refuse words, not raw status", async () => {
  const fetchMock = vi.fn(async () =>
    ({ ok: false, status: 403, json: async () => ({}) }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useModels(ADMIN, true));
  await waitFor(() => expect(view.result.current.error).not.toBeNull());
  expect(view.result.current.error).toMatch(/прав/i);
});
