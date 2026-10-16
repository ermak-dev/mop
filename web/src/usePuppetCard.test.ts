// Карточка папета (#463): GET /api/puppet/<имя> (#462) при admin-сессии --
// только по явному запросу цели, без опроса и без хранения: смена цели,
// logout, 401/403 и опоздавший ответ прежней цели не подменяют карточку.
//
// HYPOTHESIS (#463): у страницы нет приватной карточки папета, хотя контракт
// #462 уже влит: ростер публичен, факты и хвост -- только админу, и хвост
// админ тянет сам кнопкой, не фоновым опросом.
// RESULT: тесты написаны до реализации (RED).
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { usePuppetCard } from "./usePuppetCard";
import type { Puppet } from "./types";
import type { Session } from "./useSession";

const ADMIN: Session = { login: "anton", role: "admin" };
const USER: Session = { login: "bob", role: "user" };
const GUEST_SESS: Session = { login: null, role: null };

const CARD: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running", state: "busy: #12",
  kind: "busy", bucket: "busy", detail: null, branch: "#12",
  owner: "ermak", llm: "glm", origin: "git@h:g/mop.git", disk_kb: null,
};

const cardOk = (puppet: Puppet = CARD) =>
  ({ ok: true, status: 200, json: async () => ({ puppet }) }) as Response;

afterEach(() => vi.unstubAllGlobals());

test("no target: no request at all, whatever the role", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  for (const session of [ADMIN, USER, GUEST_SESS]) {
    const view = renderHook(() => usePuppetCard(session, null));
    expect(view.result.current.card).toBeNull();
    expect(view.result.current.error).toBeNull();
  }
  expect(fetchMock).not.toHaveBeenCalled();
});

test("guest and user get a synchronous refusal, the card never rides", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  for (const session of [GUEST_SESS, USER]) {
    const view = renderHook(() => usePuppetCard(session, "pu-mop-1"));
    expect(view.result.current.card).toBeNull();
    expect(view.result.current.error).toBeTruthy();
  }
  expect(fetchMock).not.toHaveBeenCalled();
});

test("admin with a target: the card arrives once, no follow-up polls", async () => {
  const fetchMock = vi.fn(async () => cardOk());
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => usePuppetCard(ADMIN, "pu-mop-1"));
  await waitFor(() => expect(view.result.current.card).toEqual(CARD));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith("/api/puppet/pu-mop-1",
    expect.objectContaining({ credentials: "same-origin" }));
});

test("404 and 502 are errors, not an empty card", async () => {
  for (const [status, word] of [[404, "нет такого папета"], [502, "панель не отвечает"]] as const) {
    const fetchMock = vi.fn(async () => ({ ok: false, status }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(() => usePuppetCard(ADMIN, "pu-none-1"));
    await waitFor(() => expect(view.result.current.error).toContain(word));
    expect(view.result.current.card).toBeNull();
  }
});

test("a late answer of the former target does not replace the card", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = vi.fn()
    .mockImplementationOnce(async () => slow)
    .mockImplementationOnce(async () => cardOk({ ...CARD, name: "pu-mop-2", owner: "alice" }));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ target }: { target: string | null }) =>
    usePuppetCard(ADMIN, target), { initialProps: { target: "pu-mop-1" } });
  // цель сменилась до ответа первой: вторая отвечает, первая ещё висит
  await act(async () => { view.rerender({ target: "pu-mop-2" }); });
  await waitFor(() => expect(view.result.current.card?.name).toBe("pu-mop-2"));
  // опоздавший ответ прежней цели приходит позже всего
  await act(async () => { release?.(cardOk()); });
  expect(view.result.current.card?.name).toBe("pu-mop-2");
  expect(view.result.current.card?.owner).toBe("alice");
});

test("logout while pending: the late answer does not resurrect the card", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = vi.fn(async () => slow);
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ session }: { session: Session }) =>
    usePuppetCard(session, "pu-mop-1"), { initialProps: { session: ADMIN } });
  await act(async () => { view.rerender({ session: GUEST_SESS }); });
  expect(view.result.current.card).toBeNull();
  // гость с целью получает синхронный отказ без фактов (контракт выше):
  // ошибку показываем, карточку -- никогда
  expect(view.result.current.error).toBeTruthy();
  await act(async () => { release?.(cardOk()); });
  expect(view.result.current.card).toBeNull();
  expect(view.result.current.error).toBeTruthy();
});

test("role degradation clears the shown card in the same render", async () => {
  const fetchMock = vi.fn(async () => cardOk());
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ session }: { session: Session }) =>
    usePuppetCard(session, "pu-mop-1"), { initialProps: { session: ADMIN } });
  await waitFor(() => expect(view.result.current.card).toEqual(CARD));
  await act(async () => { view.rerender({ session: GUEST_SESS }); });
  expect(view.result.current.card).toBeNull();
  expect(view.result.current.error).toBeTruthy();
});

test("the same target on a rerender does not refetch", async () => {
  const fetchMock = vi.fn(async () => cardOk());
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => usePuppetCard(ADMIN, "pu-mop-1"));
  await waitFor(() => expect(view.result.current.card).toEqual(CARD));
  await act(async () => { view.rerender(); });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
