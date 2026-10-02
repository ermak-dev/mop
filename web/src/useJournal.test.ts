// Журнал админа (#422): GET /api/journal (#424) при admin-сессии. Журнал
// нигде не хранится, кроме состояния страницы: 401/403/потеря роли --
// записи стираются, а не висят прежние.
//
// HYPOTHESIS (#422): страница тянет журнал отдельным запросом только когда
// session.role === "admin"; публичный снимок (#424 убрал из него journal)
// остаётся единственным источником таблиц пула.
// Ревью: пока страница журнала открыта, опрос ходит каждые POLL_S (#424
// контракта нет в SSE) -- новые события приходят без перезагрузки; на
// главной закрытый журнал не спрашивают вовсе.
// RESULT: тесты написаны до реализации (RED).
import { renderHook, act, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { POLL_S, useJournal } from "./useJournal";
import type { JournalEntry } from "./types";
import type { Session } from "./useSession";

const J: JournalEntry[] = [
  { at: 1700000000, event: "send", name: "pu-mop-1", node: "hyper", project: "mop", text: "from ermak" },
  { at: 1700000060, event: "idle", name: "pu-mop-1", node: "hyper", project: "mop", text: "" },
];

const ok = (journal: JournalEntry[]) =>
  ({ ok: true, status: 200, json: async () => ({ journal }) }) as Response;

afterEach(() => vi.unstubAllGlobals());

// Ревью (6): открытая страница журнала опрашивает /api/j -- новые события
// приходят без перезагрузки, отказ в последующем опросе стирает записи.
test("review: the open page polls, a new event arrives on the next tick", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let batch = 0;
  const fetchMock = vi.fn(async () => ok([J[batch], ...J.slice(0, batch)]));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useJournal({ login: "anton", role: "admin" }, true));
  await waitFor(() => expect(view.result.current.journal).toHaveLength(1));
  batch = 1;
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_S * 1000 + 1); });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(view.result.current.journal).toHaveLength(2);
  vi.useRealTimers();
});

test("review: a refusal on a later poll erases what was shown", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let refuse = false;
  const fetchMock = vi.fn(async () => {
    if (refuse) return { ok: false, status: 403 } as Response;
    return ok(J);
  });
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useJournal({ login: "anton", role: "admin" }, true));
  await waitFor(() => expect(view.result.current.journal).toEqual(J));
  refuse = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_S * 1000 + 1); });
  await waitFor(() => expect(view.result.current.error).toBe("журнал недоступен: прав не хватает"));
  expect(view.result.current.journal).toEqual([]);
  vi.useRealTimers();
});

test("review: the main page (journal closed) asks nothing even for an admin", async () => {
  const fetchMock = vi.fn(async () => ok(J));
  vi.stubGlobal("fetch", fetchMock);
  renderHook(() => useJournal({ login: "anton", role: "admin" }, false));
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  expect(fetchMock).not.toHaveBeenCalled();
});

// Ревью (3): поздний парс тела не возвращает журнал после logout --
// alive проверяется и после await r.json().
test("review: a late parse of a stale admin answer cannot restore the journal", async () => {
  let release: (j: JournalEntry[]) => void = () => {};
  const body = new Promise<JournalEntry[]>((r) => { release = r; });
  const fetchMock = vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ journal: await body }),
  }) as unknown as Response);
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ role }) => useJournal({ login: "anton", role } as Session, true), {
    initialProps: { role: "admin" as "admin" | null },
  });
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  // logout успел раньше ответа: роль ушла, записи стёрты
  view.rerender({ role: null });
  expect(view.result.current.journal).toEqual([]);
  release(J);   // тело старого ответа пришло уже гостю
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  expect(view.result.current.journal).toEqual([]);
});

test("admin loads the journal; a guest asks nothing", async () => {
  const fetchMock = vi.fn(async () => ok(J));
  vi.stubGlobal("fetch", fetchMock);
  const guest = renderHook(() => useJournal({ login: null, role: null }, true));
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  expect(fetchMock).not.toHaveBeenCalled();
  expect(guest.result.current.journal).toEqual([]);

  const admin = renderHook(() => useJournal({ login: "anton", role: "admin" }, true));
  await waitFor(() => expect(admin.result.current.journal).toEqual(J));
  // сигналabort -- часть вызова; проверяем остальное точно
  const [url, init] = (fetchMock as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
  expect(url).toBe("/api/journal");
  expect(init.credentials).toBe("same-origin");
  expect(init.signal).toBeInstanceOf(AbortSignal);
});

test("a plain user asks nothing either: the journal is admin-only", async () => {
  const fetchMock = vi.fn(async () => ok(J));
  vi.stubGlobal("fetch", fetchMock);
  renderHook(() => useJournal({ login: "olga", role: "user" }, true));
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  expect(fetchMock).not.toHaveBeenCalled();
});

test("losing the role erases what was loaded, not keeps it", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ok(J)));
  const view = renderHook(({ role }) => useJournal({ login: "anton", role }, true), {
    initialProps: { role: "admin" as "admin" | "user" },
  });
  await waitFor(() => expect(view.result.current.journal).toEqual(J));
  view.rerender({ role: "user" });
  // записи стираются сразу: роль ушла -- журнал больше не для этого глаза
  expect(view.result.current.journal).toEqual([]);
});

test("401 and 403 erase the journal and name the refusal; a network error names the panel", async () => {
  for (const status of [401, 403]) {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status }) as Response));
    const view = renderHook(() => useJournal({ login: "anton", role: "admin" }, true));
    await waitFor(() => expect(view.result.current.error).toBe("журнал недоступен: прав не хватает"));
    expect(view.result.current.journal).toEqual([]);
  }
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
  const view = renderHook(() => useJournal({ login: "anton", role: "admin" }, true));
  await waitFor(() => expect(view.result.current.error).toBe("журнал недоступен: панель не отвечает"));
  expect(view.result.current.journal).toEqual([]);
});

// Роль вернулась -- журнал грузится заново: после стирания никакого
// «прошлого» списка в состоянии не остаётся.
test("the role returning reloads the journal fresh", async () => {
  const fetchMock = vi.fn(async () => ok(J));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ role }) => useJournal({ login: "anton", role }, true), {
    initialProps: { role: "admin" as "admin" | null },
  });
  await waitFor(() => expect(view.result.current.journal).toEqual(J));
  view.rerender({ role: null });
  expect(view.result.current.journal).toEqual([]);
  view.rerender({ role: "admin" });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(view.result.current.journal).toEqual(J);
});
