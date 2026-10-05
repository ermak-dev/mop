// Балун терминального кадра (#509): каждый терминал операции, чей id эта
// страница не запускала (другая вкладка, другой оператор), показывается
// балуном -- слово и цвет по правилам useOperationNotice, имя цели в
// заголовке. Свой ход (id из 202 этой страницы) балуном не дублируется:
// локальный балун бокса уже показан, дедуп -- по «своим» id шины. unknown
// -- жёлтый со ссылкой в журнал внутри балуна. Кадр, пропущенный при
// свёрнутой странице, не догоняется -- известный остаток #509.
//
// HYPOTHESIS: вердикт чужой операции виден только в журнале, страница о
// нём молчала; карточки-вердикты из таблицы уходят (#509), терминал
// возвращается человеку балуном. RESULT: тесты написаны до реализации
// (RED).
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useOperationBalloons } from "./useOperationBalloons";
import { operations, VERDICT_FALLBACK_MS, type OperationFrame } from "./useOperationFeed";

// Шпион балунов: фабрика vi.mock выполняется лениво, держатель -- const.
const shown: { title?: unknown; message?: unknown; color?: unknown;
               autoClose?: unknown }[] = [];
vi.mock("@mantine/notifications", async (importOriginal) => {
  const orig = await importOriginal<typeof import("@mantine/notifications")>();
  return { ...orig, notifications: { ...orig.notifications,
    show: (n: typeof shown[number]) => shown.push(n) } };
});

const frame = (f: Partial<OperationFrame>) => act(() => {
  operations.frame({ id: "op-x", action: "restart", target: "pu-mop-1",
    status: "succeeded", ...f });
});

beforeEach(() => { shown.length = 0; });
afterEach(() => { vi.useRealTimers(); operations.resetForTest(); });

test("#509 a foreign terminal frame shows one balloon with the target name", () => {
  renderHook(() => useOperationBalloons());
  frame({ id: "op-foreign", target: "pu-mop-2", status: "succeeded" });
  expect(shown).toHaveLength(1);
  expect(String(shown[0].title)).toContain("pu-mop-2");
  expect(String(shown[0].message)).toContain("выполнено");
  expect(shown[0].color).toBe("teal");
  // правило useOperationNotice: балун живёт 8 секунд
  expect(shown[0].autoClose).toBe(8000);
});

test("#509 one click -- one balloon: my id is skipped, the box owns it", () => {
  renderHook(() => useOperationBalloons());
  // 202 этой страницы дал id -- вердикт покажет бокс операции
  act(() => { operations.accepted("pu-mop-1", "op-mine"); });
  frame({ id: "op-mine", target: "pu-mop-1", status: "succeeded" });
  expect(shown).toHaveLength(0);
});

test("#509 a targetless operation of this page is skipped too", () => {
  renderHook(() => useOperationBalloons());
  act(() => { operations.accepted(null, "op-add"); });
  frame({ id: "op-add", target: null, status: "succeeded" });
  expect(shown).toHaveLength(0);
});

test("#509 unknown is yellow and carries the journal link inside", () => {
  renderHook(() => useOperationBalloons());
  frame({ id: "op-foreign", status: "unknown" });
  expect(shown).toHaveLength(1);
  expect(shown[0].color).toBe("yellow");
  // правда об эффекте -- в журнале: ссылка едет в самом балуне
  const flat = JSON.stringify(shown[0].message);
  expect(flat).toContain("исход неизвестен");
  expect(flat).toContain("журнал");
  expect(flat).toContain("#journal");
});

test("#509 a refused frame carries the server phrase in red", () => {
  renderHook(() => useOperationBalloons());
  frame({ id: "op-foreign", status: "refused", error: "led by anton" });
  expect(shown).toHaveLength(1);
  expect(String(shown[0].message)).toContain("led by anton");
  expect(shown[0].color).toBe("red");
});

test("#509 a redelivered frame does not balloon twice", () => {
  renderHook(() => useOperationBalloons());
  frame({ id: "op-foreign", status: "succeeded" });
  frame({ id: "op-foreign", status: "succeeded" });
  expect(shown).toHaveLength(1);
});

test("#509 a lost verdict of my operation stays with the box, no balloon", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  renderHook(() => useOperationBalloons());
  act(() => { operations.accepted("pu-mop-1", "op-mine"); });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(VERDICT_FALLBACK_MS + 1000);
  });
  // таймер канала -- тот же свой id: unknown покажет бокс, не балун
  expect(shown).toHaveLength(0);
});
