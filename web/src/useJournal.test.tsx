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
import { act, render, renderHook, waitFor } from "@testing-library/react";
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

// Ревью 2 (#422): выведенный отказ -- синхронно, не эффектом. Смена
// admin→user на открытом журнале: первый же рендер после смены роли уже
// без записей -- ни одного кадра прежних данных, эффект не успел бы.
test("review2: role loss returns an empty refused journal in the same render", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ok(J)));
  const view = renderHook(({ role }) => useJournal({ login: "anton", role }, true), {
    initialProps: { role: "admin" as "admin" | "user" },
  });
  await waitFor(() => expect(view.result.current.journal).toEqual(J));
  view.rerender({ role: "user" });
  // тот же рендер, что и смена роли: записи уже нет и назван отказ
  expect(view.result.current.journal).toEqual([]);
  expect(view.result.current.error).toContain("прав не хватает");
});

// Ревью 2→3 (#422): прежняя формулировка «до нового ответа» усилена
// ревью 3 -- та же проверка живёт ниже как «in the same render»; здесь
// остаётся обращение к серверу при смене логина (новый источник).
test("review2: an admin switch asks the source anew", async () => {
  const fetchMock = vi.fn(async () => ok(J));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(({ login }) => useJournal({ login, role: "admin" }, true), {
    initialProps: { login: "anton" as string },
  });
  await waitFor(() => expect(view.result.current.journal).toEqual(J));
  view.rerender({ login: "bob" });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
});

// Ревью 2→3 (#422): поздний 200 после свежего 403 не восстанавливает
// события. Формулировка ревью 2 («отвечает последний начатый») уточнена
// ревью 3 до монотонной версии принятого -- поведение то же, тест ниже.

// Ревью 3→5 (#422): A(admin)→B(admin) -- синхронно, не эффектом. Ассерт
// срабатывает ВНУТРИ render-фазы Probe: rerender() в RTL flush'ит эффекты
// синхронно до возврата, поэтому «прочитать frames после rerender» не
// доказывает порядок. Гейт ставится до rerender, проверка первого кадра
// нового логина идёт в том же стеке, что и вызов хука.
test("review5: an admin switch wipes the old journal in the very first render", async () => {
  const AJ: JournalEntry[] = [{ at: 1, event: "send", name: "pu-a", node: "n", project: "p", text: "" }];
  const BJ: JournalEntry[] = [{ at: 2, event: "idle", name: "pu-b", node: "n", project: "p", text: "" }];
  let who = "anton";
  const fetchMock = vi.fn(async () => ok(who === "anton" ? AJ : BJ));
  vi.stubGlobal("fetch", fetchMock);
  // первый кадр с новым логином: journal хука в момент render'а
  let firstBobFrame: JournalEntry[] | undefined;
  function Probe({ login }: { login: string }) {
    const j = useJournal({ login, role: "admin" }, true);
    if (login === "bob" && firstBobFrame === undefined) firstBobFrame = j.journal;
    return null;
  }
  const view = render(<Probe login="anton" />);
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  who = "bob";
  view.rerender(<Probe login="bob" />);
  // кадр захвачен в render-фазе, ДО эффектов этого рендера
  expect(firstBobFrame).toEqual([]);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
});

// Ревью 3 (#422): медленный опрос не запирает журнал навечно. Если каждый
// ответ идёт дольше периода, слепое «позже начатый выигрывает» отбрасывало
// бы всё; применять ответ надо по монотонной версии последнего ПРИНЯТОГО:
// последовательные 200-е, каждый следующий -- свежее принятого.
test("review3: every poll slower than the period still lands its answer", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const AJ: JournalEntry[] = [{ at: 1, event: "send", name: "pu-a", node: "n", project: "p", text: "" }];
  const BJ: JournalEntry[] = [{ at: 2, event: "idle", name: "pu-b", node: "n", project: "p", text: "" }];
  let batch = 0;
  const bodies = [AJ, BJ];
  const fetchMock = vi.fn(async () => {
    const mine = batch;
    // каждый ответ приходит через полтора периода -- ПОСЛЕ следующего тика
    await new Promise((r) => setTimeout(r, POLL_S * 1000 * 1.5));
    return ok(bodies[mine]);
  });
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useJournal({ login: "anton", role: "admin" }, true));
  await act(async () => {
    batch = 1;
    await vi.advanceTimersByTimeAsync(POLL_S * 1000 * 3 + 1);
  });
  // оба ответа опоздали мимо чужого старта -- но каждый новее принятого;
  // тиков могло накопиться больше двух -- важен итог, не их число
  expect(view.result.current.journal).toEqual(BJ);
  expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  vi.useRealTimers();
});

// Ревью 3 (#422): а защита от позднего 200 после свежего 403 остаётся:
// принятый отказ -- монотонный барьер, поздний 200 мимо него не проходит.
test("review3: a late 200 after an accepted 403 still cannot land", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let calls = 0;
  let releaseFirst: (j: JournalEntry[]) => void = () => {};
  const firstBody = new Promise<JournalEntry[]>((r) => { releaseFirst = r; });
  const fetchMock = vi.fn(async () => {
    calls++;
    if (calls === 1) {
      return { ok: true, status: 200,
               json: async () => ({ journal: await firstBody }) } as unknown as Response;
    }
    if (calls === 2) return { ok: false, status: 403 } as Response;
    return ok([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useJournal({ login: "anton", role: "admin" }, true));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_S * 1000 + 1); });
  await waitFor(() => expect(view.result.current.error).toContain("прав не хватает"));
  releaseFirst(J);   // опоздавший 200 прошлого круга
  await act(async () => { await vi.advanceTimersByTimeAsync(50); });
  expect(view.result.current.journal).toEqual([]);
  vi.useRealTimers();
});

// Ревью 5 (#422): монотонный барьер -- для всех исходов. Сетевой отказ
// применяет SILENT, но не поднимал принятую версию: поздний 200 старого
// запроса затем «лечил» ошибку и возвращал записи после «панель не
// отвечает». Отказ обязан держать барьер как 401/403.
test("review5: a network refusal holds its barrier against a stale 200", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let calls = 0;
  let releaseFirst: (j: JournalEntry[]) => void = () => {};
  const firstBody = new Promise<JournalEntry[]>((r) => { releaseFirst = r; });
  const fetchMock = vi.fn(async () => {
    calls++;
    if (calls === 1) {
      // первый 200 зависает в теле
      return { ok: true, status: 200,
               json: async () => ({ journal: await firstBody }) } as unknown as Response;
    }
    if (calls === 2) throw new TypeError("network down");   // терминальный отказ
    return ok([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  const view = renderHook(() => useJournal({ login: "anton", role: "admin" }, true));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  // второй круг упал сетью -- SILENT применён
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_S * 1000 + 1); });
  await waitFor(() => expect(view.result.current.error).toContain("панель не отвечает"));
  releaseFirst(J);   // тело первого 200 пришло после отказа
  await act(async () => { await vi.advanceTimersByTimeAsync(50); });
  expect(view.result.current.journal).toEqual([]);          // не восстановился
  expect(view.result.current.error).toContain("панель не отвечает");
  vi.useRealTimers();
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
  // ждём сам журнал, не число вызовов: ответ второго fetch садится в
  // состояние асинхронно, и мгновенный чек после счётчика ловил пустой
  // рендер (мерцание в полной сборке, не в одиночном прогоне).
  await waitFor(() => expect(view.result.current.journal).toEqual(J));
});
