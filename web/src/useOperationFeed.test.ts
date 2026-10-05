// Канал вердиктов операций (#504, #509): терминал приезжает кадром SSE
// `operation` из существующего потока /events, опрос статуса уходит --
// ровно один GET сразу после 202 ловит мгновенные refused, дальше вердикт
// ждут кадром; потерянный терминал покрывает таймер (серверный потолок
// эффекта + запас). Словарь {цель -> исход} ушёл вместе с карточкой
// строки (#509): терминал возвращается человеку балуном чужого кадра
// (useOperationBalloons) и вердиктом своего бокса (useAdminOp), канал
// отвечает подпиской по id, таймерами и дедупом своих id.
//
// HYPOTHESIS (#504): страница узнавала терминал только опросом
// GET /api/admin/ops/<id>; операция с исходом unknown молчала, человек не
// видел, что исход неизвестен.
// RESULT: тесты написаны до реализации (RED); канал -- модульная шина
// (operations), соединение держит App, подписчики -- операции по id и
// балун кадра (#509).
import { act } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { onOperationEvent, operations, VERDICT_FALLBACK_MS,
         type OperationFrame } from "./useOperationFeed";

const frame = (f: Partial<OperationFrame>): OperationFrame => ({
  id: "op-1", action: "restart", target: "pu-mop-1", status: "succeeded",
  ...f,
});

const send = (f: Partial<OperationFrame>) =>
  act(() => { onOperationEvent({ data: JSON.stringify(frame(f)) } as MessageEvent); });

afterEach(() => {
  vi.useRealTimers();
  // Шина модульная: каждая проверка начинает с чистого состояния.
  operations.resetForTest();
  vi.restoreAllMocks();
});

test("#504 a terminal frame reaches id-subscribers", () => {
  const got: OperationFrame[] = [];
  const un = operations.subscribe((f) => { got.push(f); });
  send({ target: "pu-mop-1", status: "succeeded", id: "op-1" });
  expect(got).toHaveLength(1);
  expect(got[0]).toEqual(expect.objectContaining({ id: "op-1",
    target: "pu-mop-1", status: "succeeded" }));
  un();
});

test("#504 a non-terminal frame reaches no one", () => {
  const got: OperationFrame[] = [];
  const un = operations.subscribe((f) => { got.push(f); });
  send({ target: "pu-mop-1", status: "running", id: "op-1" });
  expect(got).toHaveLength(0);
  un();
});

test("#504 a lost verdict turns unknown at the deadline, not earlier", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const seen: string[] = [];
  const un = operations.subscribe((f) => seen.push(f.status));
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS - 1);
  });
  // До срока -- ни слова: честное ожидание, не преждевременный unknown.
  expect(seen.filter((s) => s === "unknown")).toHaveLength(0);
  await act(async () => { await vi.advanceTimersByTimeAsync(2); });
  expect(seen[seen.length - 1]).toBe("unknown");
  un();
});

test("#504 the SSE frame cancels the lost-verdict timer", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const seen: string[] = [];
  const un = operations.subscribe((f) => seen.push(f.status));
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  send({ target: "pu-mop-1", status: "succeeded", id: "op-1" });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS * 2);
  });
  // кадр-терминал уже был: таймер не добавляет второго слова
  expect(seen).toEqual(["succeeded"]);
  un();
});

test("#504 a terminal in the single GET cancels the timer too", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const seen: string[] = [];
  const un = operations.subscribe((f) => seen.push(f.status));
  act(() => {
    operations.accepted("pu-mop-1", "op-1");
    operations.observed("pu-mop-1", "op-1", "refused");
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS * 2);
  });
  expect(seen).toHaveLength(0);
  un();
});

test("#504 a resubmit with the same id does not restart the timer", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const seen: string[] = [];
  const un = operations.subscribe((f) => { if (f.status === "unknown") seen.push(f.status); });
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  // Повторный клик до терминала -- тот же id (идемпотентность #461):
  // таймер не сдвигается, срок остаётся от первого принятия.
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS + 1000);
  });
  expect(seen).toEqual(["unknown"]);
  un();
});

test("#504 a frame without a target reaches id-subscribers only", () => {
  const got: OperationFrame[] = [];
  const un = operations.subscribe((f) => { got.push(f); });
  send({ target: null, id: "op-add", status: "succeeded" });
  // У add цели-имени нет (#470): операция по id доходит, строки-цели нет.
  expect(got[0]?.id).toBe("op-add");
  un();
});

// add (#470) едет без цели, но операция принята этой страницей: правило
// таймера одно для всех с id из 202 -- терминал не пришёл к сроку, значит
// операция (её бокс) получает unknown.
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
  // кадр-терминал уже был: таймер не добавляет второго слова
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

// ── #509: свои id -- дедуп балунов ──────────────────────────────────────

test("#509 accepted marks the id as this page's own", () => {
  expect(operations.owns("op-1")).toBe(false);
  act(() => { operations.accepted("pu-mop-1", "op-1"); });
  expect(operations.owns("op-1")).toBe(true);
  // чужой id остаётся чужим
  expect(operations.owns("op-x")).toBe(false);
});

test("#509 a foreign frame's id is never marked own", () => {
  const seen: string[] = [];
  const un = operations.subscribe((f) => seen.push(f.id));
  send({ id: "op-foreign", status: "succeeded" });
  expect(seen).toEqual(["op-foreign"]);
  expect(operations.owns("op-foreign")).toBe(false);
  un();
});
