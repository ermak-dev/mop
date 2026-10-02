// Журнал админа (#422): загрузка закрытого GET /api/journal (сервер #424)
// при admin-сессии #415/#416. Журнал нигде не хранится, кроме состояния
// страницы: 401/403/потеря роли -- записи стираются, а не висят прежние.
//
// HYPOTHESIS (#422): страница тянет журнал отдельным запросом только когда
// session.role === "admin"; публичный снимок (#424 убрал из него journal)
// остаётся единственным источником таблиц пула.
// RESULT: тесты написаны до реализации (RED).
import { renderHook, act, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useJournal } from "./useJournal";
import type { JournalEntry } from "./types";

const J: JournalEntry[] = [
  { at: 1700000000, event: "send", name: "pu-mop-1", node: "hyper", project: "mop", text: "from ermak" },
  { at: 1700000060, event: "idle", name: "pu-mop-1", node: "hyper", project: "mop", text: "" },
];

const ok = (journal: JournalEntry[]) =>
  ({ ok: true, status: 200, json: async () => ({ journal }) }) as Response;

afterEach(() => vi.unstubAllGlobals());

test("admin loads the journal; a guest asks nothing", async () => {
  const fetchMock = vi.fn(async () => ok(J));
  vi.stubGlobal("fetch", fetchMock);
  const guest = renderHook(() => useJournal({ login: null, role: null }));
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  expect(fetchMock).not.toHaveBeenCalled();
  expect(guest.result.current.journal).toEqual([]);

  const admin = renderHook(() => useJournal({ login: "anton", role: "admin" }));
  await waitFor(() => expect(admin.result.current.journal).toEqual(J));
  expect(fetchMock).toHaveBeenCalledWith("/api/journal", { credentials: "same-origin" });
});

test("a plain user asks nothing either: the journal is admin-only", async () => {
  const fetchMock = vi.fn(async () => ok(J));
  vi.stubGlobal("fetch", fetchMock);
  renderHook(() => useJournal({ login: "olga", role: "user" }));
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  expect(fetchMock).not.toHaveBeenCalled();
});

test("losing the role erases what was loaded, not keeps it", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ok(J)));
  const view = renderHook(({ role }) => useJournal({ login: "anton", role }), {
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
    const view = renderHook(() => useJournal({ login: "anton", role: "admin" }));
    await waitFor(() => expect(view.result.current.error).toBe("журнал недоступен: прав не хватает"));
    expect(view.result.current.journal).toEqual([]);
  }
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
  const view = renderHook(() => useJournal({ login: "anton", role: "admin" }));
  await waitFor(() => expect(view.result.current.error).toBe("журнал недоступен: панель не отвечает"));
  expect(view.result.current.journal).toEqual([]);
});

// Роль вернулась -- журнал грузится заново: после стирания никакого
// «прошлого» списка в состоянии не остаётся.
test("the role returning reloads the journal fresh", async () => {
  const fetchMock = vi.fn(async () => ok(J));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ role }) => useJournal({ login: "anton", role }), {
    initialProps: { role: "admin" as "admin" | null },
  });
  await waitFor(() => expect(view.result.current.journal).toEqual(J));
  view.rerender({ role: null });
  expect(view.result.current.journal).toEqual([]);
  view.rerender({ role: "admin" });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(view.result.current.journal).toEqual(J);
});
