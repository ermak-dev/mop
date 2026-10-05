// Канал вердиктов операций (#504): терминал приезжает кадром SSE
// `operation` из существующего потока /events, опрос статуса уходит --
// ровно один GET сразу после 202, дальше вердикт ждут кадром; потерянный
// терминал покрывает таймер (серверный потолок эффекта + запас).
//
// HYPOTHESIS (#504): страница узнавала терминал только опросом
// GET /api/admin/ops/<id>; операция с исходом unknown молчала, человек не
// видел, что исход неизвестен.
// RESULT: тесты написаны до реализации (RED); канал -- модульная шина
// (operations), соединение держит App, подписчики -- таблица и операции.
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { onOperationEvent, operations, useTargetVerdicts,
         VERDICT_FALLBACK_MS, type OperationFrame } from "./useOperationFeed";

const frame = (f: Partial<OperationFrame>): OperationFrame => ({
  id: "op-1", action: "restart", target: "pu-mop-1", status: "succeeded",
  ...f,
});

const send = (f: Partial<OperationFrame>) =>
  act(() => { onOperationEvent({ data: JSON.stringify(frame(f)) } as MessageEvent); });

afterEach(() => {
  vi.useRealTimers();
  // Шина модульная: каждая проверка начинает с чистого словаря.
  operations.resetForTest();
  vi.restoreAllMocks();
});

test("#504 a terminal frame lands on its target and is read by the row", () => {
  const h = renderHook(() => useTargetVerdicts());
  send({ target: "pu-mop-1", status: "succeeded", id: "op-1" });
  expect(h.result.current["pu-mop-1"]).toEqual(
    expect.objectContaining({ status: "succeeded", id: "op-1",
                              lostVerdict: false }));
  // Цель без операций -- ключа нет, а не undefined-статус.
  expect(h.result.current["pu-mop-2"]).toBeUndefined();
});

test("#504 a duplicate frame for the same id does not resurrect a stale verdict", () => {
  const h = renderHook(() => useTargetVerdicts());
  send({ target: "pu-mop-1", status: "unknown", id: "op-1" });
  expect(h.result.current["pu-mop-1"].status).toBe("unknown");
  // Дубль кадра того же id с чужим статусом -- артефакт доставки:
  // терминал у операции один, стоящий вердикт не перезаписывается.
  send({ target: "pu-mop-1", status: "succeeded", id: "op-1" });
  expect(h.result.current["pu-mop-1"].status).toBe("unknown");
});

test("#504 a non-terminal frame changes nothing", () => {
  const h = renderHook(() => useTargetVerdicts());
  send({ target: "pu-mop-1", status: "running", id: "op-1" });
  expect(h.result.current["pu-mop-1"]).toBeUndefined();
});

test("#504 the single GET feeds the same map: waiting now, terminal settles", () => {
  const h = renderHook(() => useTargetVerdicts());
  act(() => { operations.observed("pu-mop-1", "op-1", "waiting", null); });
  expect(h.result.current["pu-mop-1"].status).toBe("waiting");
  act(() => { operations.observed("pu-mop-1", "op-1", "refused", "led by anton"); });
  expect(h.result.current["pu-mop-1"].status).toBe("refused");
  expect(h.result.current["pu-mop-1"].error).toBe("led by anton");
});

test("#504 a lost verdict turns unknown at the deadline, not earlier", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const seen: string[] = [];
  const h = renderHook(() => useTargetVerdicts());
  h.result.current; // подписка состоялась
  const un = operations.subscribe((f) => seen.push(f.status));
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS - 1);
  });
  // До срока -- ни слова: честное ожидание, не преждевременный unknown.
  expect(seen.filter((s) => s === "unknown")).toHaveLength(0);
  await act(async () => { await vi.advanceTimersByTimeAsync(2); });
  expect(seen[seen.length - 1]).toBe("unknown");
  expect(h.result.current["pu-mop-1"].lostVerdict).toBe(true);
  un();
});

test("#504 the SSE frame cancels the lost-verdict timer", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const h = renderHook(() => useTargetVerdicts());
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  send({ target: "pu-mop-1", status: "succeeded", id: "op-1" });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS * 2);
  });
  expect(h.result.current["pu-mop-1"].status).toBe("succeeded");
  expect(h.result.current["pu-mop-1"].lostVerdict).toBe(false);
});

test("#504 a terminal in the single GET cancels the timer too", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const h = renderHook(() => useTargetVerdicts());
  act(() => {
    operations.accepted("pu-mop-1", "op-1");
    operations.observed("pu-mop-1", "op-1", "refused", null);
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS * 2);
  });
  expect(h.result.current["pu-mop-1"].status).toBe("refused");
});

test("#504 an unknown frame arrives instantly: no anti-flashing delay", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const h = renderHook(() => useTargetVerdicts());
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  send({ target: "pu-mop-1", status: "unknown", id: "op-1" });
  // Кадр unknown -- вердикт сразу, ждать срока не нужно.
  expect(h.result.current["pu-mop-1"].status).toBe("unknown");
  expect(h.result.current["pu-mop-1"].lostVerdict).toBe(false);
});

test("#504 frames for targets without a page operation are still recorded", () => {
  const h = renderHook(() => useTargetVerdicts());
  // Операция другого мастера/вкладки: id странице не знаком, цель -- та же
  // строка таблицы; вердикт цели обязан быть виден.
  send({ target: "pu-mop-1", status: "unknown", id: "op-x" });
  expect(h.result.current["pu-mop-1"].status).toBe("unknown");
  // Чужой id, та же цель -- вердикт цели обновился (дедуп -- по id, не по цели).
  send({ target: "pu-mop-1", status: "succeeded", id: "op-y" });
  expect(h.result.current["pu-mop-1"].status).toBe("succeeded");
});

test("#504 a resubmit with the same id does not restart the timer", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  renderHook(() => useTargetVerdicts());
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  // Повторный клик до терминала -- тот же id (идемпотентность #461):
  // таймер не сдвигается, срок остаётся от первого принятия.
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  let fired = false;
  const un = operations.subscribe((f) => { if (f.status === "unknown") fired = true; });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS + 1000);
  });
  expect(fired).toBe(true);
  un();
});

test("#504 a frame without a target reaches id-subscribers only", () => {
  const got: OperationFrame[] = [];
  const un = operations.subscribe((f) => { got.push(f); });
  send({ target: null, id: "op-add", status: "succeeded" });
  // У add цели-имени нет (#470): операция по id доходит, словарь целей молчит.
  expect(got[0]?.id).toBe("op-add");
  const h = renderHook(() => useTargetVerdicts());
  expect(h.result.current["pu-mop-1"]).toBeUndefined();
  un();
});

// add (#470) едет без цели, но операция принята этой страницей: правило
// таймера одно для всех с id из 202 -- терминал не пришёл к сроку, значит
// операция (её бокс) получает unknown. Строки-цели нет -- словарь молчит.
test("#504 a lost targetless verdict reaches the operation at the deadline", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const seen: string[] = [];
  const un = operations.subscribe((f) => { if (f.id === "op-add") seen.push(f.status); });
  act(() => { operations.accepted(null, "op-add"); });
  await act(async () => { await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS - 1); });
  expect(seen).toHaveLength(0);      // до срока -- честное ожидание
  await act(async () => { await vi.advanceTimersByTimeAsync(2); });
  expect(seen).toEqual(["unknown"]);
  un();
});

test("#504 a terminal frame cancels the targetless timer too", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const seen: string[] = [];
  const un = operations.subscribe((f) => { if (f.id === "op-add") seen.push(f.status); });
  act(() => { operations.accepted(null, "op-add"); });
  send({ target: null, id: "op-add", status: "succeeded" });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS * 2);
  });
  // Кадр-терминал уже был: таймер не добавляет второго слова.
  expect(seen).toEqual(["succeeded"]);
  un();
});

test("#504 an expired record cancels the targetless timer too", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const seen: string[] = [];
  const un = operations.subscribe((f) => { if (f.id === "op-add") seen.push(f.status); });
  act(() => { operations.accepted(null, "op-add"); });
  // Единственный GET принёс «not found» (запись вытекла): правда -- журнал.
  act(() => { operations.expired(null, "op-add"); });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS * 2);
  });
  expect(seen).toHaveLength(0);      // вытекшая запись не рождает кадр
  un();
});
