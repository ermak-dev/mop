// #522: визард «Правка» -- единственный диалог операций цели, все
// доп. диалоги -- его экраны. Единое правило подтверждения: КАЖДАЯ
// операция (включая обратимые -- рестарт, «продолжить как было»,
// модель) заканчивается финальным экраном «что произойдёт», и этот
// экран сам является подтверждением: кнопка «Завершить» = согласие.
// Никаких immediate-POST и шагов подтверждения не существует.
// Первый экран: шесть пунктов; при выборе «Обновление» под ним
// inline -- радио трёх режимов #470 (+поле origin у пересадки), тот же
// шаг Степпера. Шаг параметров -- только у модели (каталог #468).
// После «Завершить» визард не закрывается, а показывает экран хода:
// Loader+слово в живом, терминальный вердикт с причиной; футер
// «Новая команда» (по терминалу) и «Закрыть». Модель -- исключение
// #488: вердикт -- балун, визард закрывается сразу. Первый экран
// однотонный: красный/оранжевый ушли, опасность говорит текстом
// финального экрана; дизейбл рестарта с причиной #508 остаётся.
// Гард #473: смена цели сбрасывает к выбору. Назад ничего не очищает.
//
// RESULT: тесты написаны до реализации (RED).
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { ControlWindow, type EngineState } from "./ControlWindow";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

// llm -- из каталога ниже: пункт «текущая» проверяется на шаге модели
const ROW: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "busy: #12", kind: "busy", bucket: "busy", detail: null,
  branch: "#12", owner: "ermak", llm: "glm-5.3-flash",
  origin: "git@h:g/mop.git", disk_kb: null,
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Открыть визард со шпионами приказа и закрытия. */
const mount = (row: Puppet = ROW,
               engineState: Record<string, EngineState> = {}) => {
  const onOrder = vi.fn();
  const onClose = vi.fn();
  const view = renderUi(<ControlWindow opened row={row} session={ADMIN}
    onClose={onClose} onOrder={onOrder} engineState={engineState} />);
  return { view, onOrder, onClose };
};

/** Текущий экран визарда: живой ход после «Завершить» даёт словом
 * статус из поднятого стейта, тест его подставляет ререндером. */
const remount = (view: ReturnType<typeof mount>,
                 engineState: Record<string, EngineState>) =>
  view.view.rerender(<MantineProvider>
    <ControlWindow opened row={ROW} session={ADMIN}
      onClose={vi.fn()} onOrder={vi.fn()} engineState={engineState} />
  </MantineProvider>);

test("#522 the choice screen lists six actions, update is named Обновление", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  const win = screen.getByTestId("control-window");
  for (const label of ["Обновление", "Модель", "Рестарт", "Пересоздать", "Удалить", "Сбросить"]) {
    expect(within(win).getByRole("button", { name: label })).toBeInTheDocument();
  }
  expect(within(win).queryByRole("button", { name: "Правка" })).toBeNull();
  expect(onOrder).not.toHaveBeenCalled();
});

// Первый экран однотонный: опасность говорит текстом финального экрана,
// не цветом пункта.
test("#522 the choice screen carries no danger colors", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  const win = screen.getByTestId("control-window");
  const items = ["Пересоздать", "Удалить", "Сбросить", "Рестарт"]
    .map((l) => within(win).getByRole("button", { name: new RegExp(l) }));
  for (const item of items) {
    expect(item.className).not.toMatch(/(--color-danger|--color-red|c-red|color-red)/i);
  }
});

// Радио режимов -- inline на ПЕРВОМ экране под пунктом «Обновление»,
// шаг выбора режима ушел: тот же шаг Степпера.
test("#522 update modes ride inline on the choice screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  // окно живо, радио -- в том же окне, рядом с пунктами
  const win = screen.getByTestId("control-window");
  for (const m of ["продолжить как было", "свежий клон, тот же origin", "другой origin"]) {
    expect(within(win).getByRole("radio", { name: new RegExp(m, "i") })).toBeInTheDocument();
  }
  // пункты никуда не делись: выбор можно сменить одним кликом
  expect(within(win).getByRole("button", { name: "Модель" })).toBeInTheDocument();
  expect(onClose).not.toHaveBeenCalled();
});

// Смена пункта убирает радио: режимы относятся только к «Обновление».
test("#522 picking another item folds the update radios away", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200,
    json: async () => ({ models: [] }) }) as Response));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  expect(screen.getByRole("radio", { name: /как было/i })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Модель" }));
  expect(screen.queryByRole("radio", { name: /как было/i })).toBeNull();
  // шаг модели -- каталог, радио моделей другого словаря
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
});

// Рестарт -- через финальный экран: immediate-POST #513/#520 ушёл,
// единое правило подтверждения #522. Никакого POST до «Завершить».
test("#522 the restart item rides its final screen, never an immediate post", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose, onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Рестарт" }));
  expect(onOrder).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  const win = screen.getByTestId("control-window");
  // финальный экран рестарта: последствия RESTART_EFFECTS + факты цели
  expect(win.textContent).toMatch(/Клон не трогается: несохранённая работа живёт/);
  expect(win.textContent).toMatch(/Разговор теряется/);
  expect(win.textContent).toContain("pu-mop-1");
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "restart", params: {} }));
});

// Дизейбл-причина #508 -- на пункте первого экрана: признак строки
// виден до клика, отказ после клика был бы вторым ответом.
test.each([
  ["down", "рестарт недоступен: не запущен"],
  ["failing", "рестарт недоступен: задача падает"],
])("#522 the restart item carries the %s disable reason", (kind, reason) => {
  vi.stubGlobal("fetch", vi.fn());
  const row: Puppet = kind === "down"
    ? { ...ROW, alloc_status: "stopped", bucket: "down", state: "stopped" }
    : { ...ROW, kind: "failing", state: "FAILED: exit 1", detail: "exit 1" };
  const { onOrder } = mount(row);
  const item = screen.getByRole("button", { name: new RegExp(reason) });
  expect(item).toBeDisabled();
  expect(item).toHaveAttribute("aria-label", reason);
  expect(onOrder).not.toHaveBeenCalled();
});

// keep обратим, но финальный экран обязателен всем (#522): «Продолжить»
// с первого экрана ведёт на финал, «Завершить» шлёт приказ.
test("#522 update keep rides the final screen before the order", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose, onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /как было/i }));
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/Разговор продолжится: контекст сессии остаётся/);
  expect(onOrder).not.toHaveBeenCalled();
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "update", params: { fresh: false } }));
  expect(onClose).not.toHaveBeenCalled();
});

// replace: финальный экран -- последствия И факты точной цели одной
// страницей, одна кнопка согласия.
test("#522 update replace rides the final screen with target facts", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /свежий клон/i }));
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/несохранённая работа погибнет/);
  expect(win.textContent).toContain("pu-mop-1");
  expect(win.textContent).toContain("hyper");
  expect(win.textContent).toContain("busy: #12");
  expect(within(win).getAllByRole("button", { name: /^Завершить$/ })).toHaveLength(1);
  expect(onOrder).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "update", params: { fresh: true } }));
});

test("#522 update remove requires the origin and posts it", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /другой origin/i }));
  // пустой origin запирает путь дальше: пересадка без адреса не едет
  expect(screen.getByRole("button", { name: /^Продолжить$/ })).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/новый origin/i),
    { target: { value: "https://x/y/other.git" } });
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  expect(screen.getByTestId("control-window").textContent)
    .toContain("https://x/y/other.git");
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "update", params: { fresh: true, origin: "https://x/y/other.git" } }));
});

// Каталог -- единственный источник (#468): визард спрашивает сервер,
// текущая модель мечена и заперта; модель -- три шага: выбор ->
// параметры -> финал.
test("#522 model rides the catalog, the current model is locked, three steps", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200,
    json: async () => ({ models: ["glm-5.3", "glm-5.3-flash"] }) }) as Response));
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Модель" }));
  const radio = await screen.findByRole("radio", { name: /^glm-5\.3$/i });
  expect(screen.getByRole("radio", { name: /текущая/i })).toBeDisabled();
  // без выбора дальше пути нет
  expect(screen.getByRole("button", { name: /^Продолжить$/ })).toBeDisabled();
  fireEvent.click(radio);
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toContain("glm-5.3");
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "model", params: { model: "glm-5.3" } }));
});

// Разрушительное -- выбор сразу на финальный экран: последствия и
// факты цели вместе, одна «Завершить».
test("#522 destructive rides straight to the final screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Пересоздать" }));
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/несохранённая работа погибнет/);
  expect(win.textContent).toMatch(/сила \(force\) не применяется/);
  expect(win.textContent).toContain("pu-mop-1");
  expect(win.textContent).toContain("hyper");
  expect(win.textContent).toContain("busy: #12");
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "recycle", params: {} }));
});

// Назад ничего не очищает: выбранный режим, origin и модель живут.
test("#522 back keeps the chosen mode and origin", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /другой origin/i }));
  fireEvent.change(screen.getByLabelText(/новый origin/i),
    { target: { value: "https://x/y/other.git" } });
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  fireEvent.click(screen.getByRole("button", { name: /^Назад$/ }));
  // режим и адрес на месте: повторный проход не начинает с нуля
  expect((screen.getByRole("radio", { name: /другой origin/i }) as HTMLInputElement)
    .checked).toBe(true);
  expect(screen.getByLabelText(/новый origin/i)).toHaveValue("https://x/y/other.git");
});

test("#522 back from the final screen returns to the choice", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Сбросить" }));
  expect(screen.getByTestId("control-window").textContent).toMatch(/Тело пересоздаётся/);
  fireEvent.click(screen.getByRole("button", { name: /^Назад$/ }));
  for (const label of ["Обновление", "Модель", "Рестарт", "Пересоздать", "Удалить", "Сбросить"]) {
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  }
});

// После «Завершить» визард НЕ закрывается: экран хода. Живой ход --
// Loader и слово статуса; футер хода -- только «Закрыть» (повтор --
// «Новая команда» -- по терминалу).
test("#522 finishing switches to the progress screen, not close", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Сбросить" }));
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  const win = screen.getByTestId("control-window");
  expect(win).toBeInTheDocument();
  expect(onClose).not.toHaveBeenCalled();
  expect(within(win).getByRole("status")).toBeInTheDocument();
  // футер хода: нет «новой команды» до терминала; «Закрыть» -- кнопка
  // футера, крест модала тоже «Закрыть» -- считаем внутри окна
  expect(within(win).getAllByRole("button", { name: /^Закрыть$/ }).length)
    .toBeGreaterThanOrEqual(1);
  expect(within(win).queryByRole("button", { name: "Новая команда" })).toBeNull();
});

// Терминальный вердикт -- тоже экран визарда: слово, причина и
// «(первый ответ потерян)»; «Новая команда» возвращает к выбору.
test("#522 a terminal verdict lands on the wizard screen with the reason", async () => {
  vi.stubGlobal("fetch", vi.fn());
  const held = mount(ROW, {});
  fireEvent.click(screen.getByRole("button", { name: "Сбросить" }));
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  // движок доложил терминал: {status, error, lost} одним объектом
  remount(held, { destructive: { status: "failed",
    error: "image builder refused" } });
  const win = await screen.findByTestId("control-window");
  expect(win.textContent).toMatch(/Не удался/);
  expect(win.textContent).toContain("image builder refused");
  fireEvent.click(within(win).getByRole("button", { name: "Новая команда" }));
  // новая команда -- с выбора: все шесть пунктов снова
  for (const label of ["Обновление", "Модель", "Рестарт"]) {
    expect(within(win).getByRole("button", { name: label })).toBeInTheDocument();
  }
});

test("#522 a lost first response is named on the verdict screen", async () => {
  vi.stubGlobal("fetch", vi.fn());
  const held = mount(ROW, {});
  fireEvent.click(screen.getByRole("button", { name: "Рестарт" }));
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  remount(held, { restart: { status: "unknown", lost: true } });
  const win = await screen.findByTestId("control-window");
  expect(win.textContent).toMatch(/Исход неизвестен/);
  expect(win.textContent).toMatch(/первый ответ потерян/);
});

// Повторный вход при непоказанном ходе (#487) -- сразу экран вердикта:
// divert и reveal-токены ушли, визард читает поднятый стейт.
// «Новая команда» (#522) обязана чистить и поднятый стейт движка: иначе
// pick того же пункта после вердикта diverted бы обратно на экран хода
// навсегда -- терминальный стейт пережил бы сброс визарда.
test("#522 the again command asks the engine to forget the verdict", () => {
  vi.stubGlobal("fetch", vi.fn());
  const onForget = vi.fn();
  renderUi(<ControlWindow opened row={ROW} session={ADMIN} onClose={vi.fn()}
    onOrder={vi.fn()} onForget={onForget}
    engineState={{ restart: { status: "failed", error: "alloc refused" } }} />);
  fireEvent.click(within(screen.getByTestId("control-window"))
    .getByRole("button", { name: "Новая команда" }));
  expect(onForget).toHaveBeenCalledTimes(1);
});

test("#522 re-entry with a live engine state opens the verdict screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount(ROW, { restart: { status: "running" } });
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/Идёт/);
  expect(within(win).getByRole("status")).toBeInTheDocument();
  expect(onOrder).not.toHaveBeenCalled();
});

// Гард #473 в визарде: смена цели под открытым окном (после выбора
// пункта) сбрасывает к выбору.
test("#522 a target change under an open wizard resets to the choice screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { view, onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Сбросить" }));
  expect(screen.getByTestId("control-window").textContent).toMatch(/Тело пересоздаётся/);
  view.rerender(<MantineProvider>
    <ControlWindow opened row={{ ...ROW, name: "pu-mop-2" }} session={ADMIN}
      onClose={vi.fn()} onOrder={vi.fn()} />
  </MantineProvider>);
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toContain("pu-mop-2");
  expect(screen.getByRole("button", { name: "Обновление" })).toBeInTheDocument();
  expect(onOrder).not.toHaveBeenCalled();
});

// ЕДИНЫЙ диалог: визард не открывает вторую моду -- вердикты живут
// его экраном; контроль по числу диалогов на терминальном ходу.
test("#522 the wizard is the only dialog, verdicts never open a second one", async () => {
  vi.stubGlobal("fetch", vi.fn());
  const held = mount(ROW, {});
  fireEvent.click(screen.getByRole("button", { name: "Сбросить" }));
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  remount(held, { destructive: { status: "failed" } });
  await screen.findByText(/Не удался/);
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
});
