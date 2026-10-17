// #522: визард «Правка» -- единственный диалог операций цели, все
// доп. диалоги -- его экраны. Единое правило подтверждения: КАЖДАЯ
// операция (включая обратимые -- рестарт, «продолжить работу»,
// модель) заканчивается финальным экраном «что произойдёт», и этот
// экран сам является подтверждением: кнопка «Завершить» = согласие.
// Никаких immediate-POST и шагов подтверждения не существует.
// Первый экран: шесть пунктов (#520, каркас не тронут #527). #527:
// «Обновление» ведёт в собственный ТРЁХШАГОВЫЙ поток (выбор режима →
// параметры/последствия → финал) -- радио режимов уходит с первого
// экрана, смешения радио с кнопками чужих операций больше нет.
// Шаг параметров -- у модели (каталог #468) и обновления (origin у
// переключения на другой проект; «продолжить работу» шаг пропускает).
// После «Завершить» визард не закрывается, а показывает экран хода:
// Loader+слово в живом, терминальный вердикт с причиной; футер
// «Новая команда» (по терминалу) и «Закрыть». Модель -- исключение
// #488: вердикт -- балун, визард закрывается сразу. Первый экран
// однотонный: красный/оранжевый ушли, опасность говорит текстом
// финального экрана; дизейбл рестарта с причиной #508 остаётся.
// Гард #473: смена цели сбрасывает к выбору. Назад ничего не очищает.
// #527: словарь замен (репозиторий, рабочая копия, контекст диалога с
// Claude, задача), «сила (force) не применяется» ушла, «частичный
// исход» упрощена; title «Правка папета», size lg, футер шагов
// [Отмена/Назад] [Продолжить/Завершить]; фиксированная высота шага --
// структурный minHeight по максимуму содержания.
//
// RESULT: тесты написаны до реализации (RED).
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { ControlWindow, type EngineState } from "./ControlWindow";
import { LABELS } from "./labels";
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

// #627: потоки визарда проверяются у каждого пункта, в том числе у тех,
// которых контроллер ещё не принимает (update, model, recycle) -- они
// вернутся вместе с командами. Поэтому здесь допуск -- «всё принимается»;
// боевой допуск по таблице store проверяется отдельно ниже.
const ALL = () => true;

/** Открыть визард со шпионами приказа и закрытия. */
const mount = (row: Puppet = ROW,
               engineState: Record<string, EngineState> = {}) => {
  const onOrder = vi.fn();
  const onClose = vi.fn();
  const view = renderUi(<ControlWindow opened row={row} session={ADMIN} admits={ALL}
    onClose={onClose} onOrder={onOrder} engineState={engineState} />);
  return { view, onOrder, onClose };
};

/** Текущий экран визарда: живой ход после «Завершить» даёт словом
 * статус из поднятого стейта, тест его подставляет ререндером. */
const remount = (view: ReturnType<typeof mount>,
                 engineState: Record<string, EngineState>) =>
  view.view.rerender(<MantineProvider>
    <ControlWindow opened row={ROW} session={ADMIN} admits={ALL}
      onClose={vi.fn()} onOrder={vi.fn()} engineState={engineState} />
  </MantineProvider>);

// HYPOTHESIS: визард предлагал обновление, модель и пересоздание, которых
// контроллер не принимает: обновление и пересоздание доходили до «Завершить»
// и только там получали отказ, шаг модели звал каталог /api/admin/models,
// которого у контроллера нет.
// SOLUTION: допуск пункта -- таблица команд общего store; непринятый пункт
// виден, но выключен с причиной, как рестарт у незапущенного папета (#508).
test("#627 the choice screen disables what the controller does not admit", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<ControlWindow opened row={ROW} session={ADMIN} onClose={vi.fn()} onOrder={vi.fn()} />);
  const win = screen.getByTestId("control-window");
  for (const label of [LABELS.update, LABELS.model, LABELS.recycle]) {
    const item = within(win).getByRole("button", { name: new RegExp(`^${label}`) });
    expect(item).toBeDisabled();
    expect(item).toHaveAccessibleName(expect.stringContaining("контроллер эту команду ещё не принимает"));
  }
  for (const label of [LABELS.restart, LABELS.stop, LABELS.delete, LABELS.wipe])
    expect(within(win).getByRole("button", { name: label })).toBeEnabled();
  expect(vi.mocked(fetch)).not.toHaveBeenCalled();
});

test("#522 the choice screen lists six actions, update is named Обновление", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  const win = screen.getByTestId("control-window");
  for (const label of ["Обновление", "Модель", "Рестарт", "Пересоздать", "Удалить", "Сбросить"]) {
    expect(within(win).getByRole("button", { name: label })).toBeInTheDocument();
  }
  expect(within(win).queryByRole("button", { name: "Правка" })).toBeNull();
  // #527: радио режимов на первом экране нет -- у обновления свой шаг
  expect(within(win).queryAllByRole("radio")).toHaveLength(0);
  // #527: title «Правка папета» и size lg на всех экранах визарда;
  // Mantine рендерит size переменной --modal-size на корне модала
  expect(within(win).getByText("Правка папета")).toBeInTheDocument();
  const root = win.closest(".mantine-Modal-root") ?? win;
  expect((root as HTMLElement).style.getPropertyValue("--modal-size"))
    .toBe("var(--modal-size-lg)");
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

// Радио режимов (#527) -- на СВОЁМ шаге потока обновления, не на
// первом экране: смешения радио с кнопками чужих операций больше нет.
// Вход: «Обновление» → шаг «Что вы хотите сделать с папетом?».
test("#527 update rides its own mode step, radios off the choice screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose } = mount();
  // первый экран: шесть действий, радио на нём нет
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  const win = screen.getByTestId("control-window");
  // шаг выбора режима: заголовок из тикета, три радио с описаниями
  expect(within(win).getByText("Что вы хотите сделать с папетом?")).toBeInTheDocument();
  for (const m of ["Продолжить работу", "Пересобрать чистый клон",
                   "Переключить на другой проект"]) {
    expect(within(win).getByRole("radio", { name: new RegExp(m, "i") })).toBeInTheDocument();
  }
  // чужих операций на этом шаге нет -- радио не смешан с кнопками
  expect(within(win).queryByRole("button", { name: "Модель" })).toBeNull();
  expect(within(win).queryByRole("button", { name: "Рестарт" })).toBeNull();
  // футер шага: [Отмена] [Продолжить], без «Завершить»
  expect(within(win).getByRole("button", { name: LABELS.cancel })).toBeInTheDocument();
  expect(within(win).getAllByRole("button", { name: LABELS.next })).toHaveLength(1);
  expect(within(win).queryByRole("button", { name: /^Завершить$/ })).toBeNull();
  expect(onClose).not.toHaveBeenCalled();
});

// Радио трёх режимов -- с краткими описаниями под каждым (#527):
// статичная подпись видна у всех трёх до выбора; полные последствия
// живут на шаге параметров, не на выборе.
test("#527 the mode radios carry their consequence descriptions", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  const win = screen.getByTestId("control-window");
  // подпись-описание -- у всех трёх радио, канон тикета дословно
  expect(win.textContent).toMatch(/Папет продолжит с того же места/);
  expect(win.textContent).toMatch(/Папет начнёт с чистой копии кода/);
  expect(win.textContent).toMatch(/Папет начнёт работать с другим репозиторием/);
  // «Продолжить» заперт до выбора режима
  expect(within(win).getByRole("button", { name: LABELS.next })).toBeDisabled();
  fireEvent.click(within(win).getByRole("radio", { name: /Пересобрать чистый клон/i }));
  expect(within(win).getByRole("button", { name: LABELS.next })).toBeEnabled();
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
  expect(win.textContent).toMatch(/Рабочая копия не трогается: несохранённая работа живёт/);
  expect(win.textContent).toMatch(/Контекст диалога с Claude теряется/);
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

// keep обратим, но финальный экран обязателен всем (#522), а шаг
// параметров он пропускает (#527: у keep нет ни параметров, ни
// последствий): выбор режима → сразу финал, «Завершить» шлёт приказ.
test("#527 update keep skips the parameter step straight to the final", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose, onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /Продолжить работу/i }));
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  const win = screen.getByTestId("control-window");
  // финал канона: факты + режим, БЕЗ блока «Внимание:» -- keep обратим
  expect(within(win).getByText("Проверьте параметры операции")).toBeInTheDocument();
  expect(win.textContent).toMatch(/Операция: Продолжить работу/);
  expect(win.textContent).not.toMatch(/Внимание:/);
  expect(onOrder).not.toHaveBeenCalled();
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "update", params: { fresh: false } }));
  expect(onClose).not.toHaveBeenCalled();
});

// replace: шаг параметров -- «Что произойдёт:» с буллетами канона,
// затем финальный экран с фактами и блоком «Внимание:».
test("#522 update replace rides the final screen with target facts", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /Пересобрать чистый клон/i }));
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  // шаг параметров: буллеты «Что произойдёт:» (replace без поля)
  let win = screen.getByTestId("control-window");
  expect(within(win).getByText("Что произойдёт:")).toBeInTheDocument();
  expect(win.textContent).toMatch(/Рабочая копия будет пересобрана с нуля/);
  expect(win.textContent).toMatch(/Папет останется на том же проекте/);
  expect(within(win).queryByRole("textbox")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  // финал: факты канона + «Внимание:» -- replace необратим
  win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/Папет: pu-mop-1/);
  expect(win.textContent).toMatch(/Узел: hyper/);
  expect(win.textContent).toMatch(/Текущий статус: busy: #12/);
  expect(win.textContent).toMatch(/Операция: Пересобрать чистый клон/);
  expect(win.textContent).toMatch(/Внимание:/);
  expect(win.textContent).toMatch(/Контекст диалога будет потерян/);
  expect(win.textContent).toMatch(/Первая сборка после пересоздания займёт больше времени/);
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
  fireEvent.click(screen.getByRole("radio", { name: /Переключить на другой проект/i }));
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  // шаг параметров: пустой проект запирает путь дальше --
  // переключение без адреса не едет; имя цели названо на шаге
  expect(screen.getByRole("button", { name: /^Продолжить$/ })).toBeDisabled();
  expect(screen.getByTestId("control-window").textContent)
    .toMatch(/Имя папета останется прежним: pu-mop-1/);
  fireEvent.change(screen.getByLabelText(/новый проект/i),
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
// факты цели вместе, одна «Завершить». #527: «сила (force) не
// применяется» ушла вовсе -- у всех действий один и тот же отказ от
// force, повторять его текстом бессмысленно.
test("#522 destructive rides straight to the final screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Пересоздать" }));
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/несохранённая работа погибнет/);
  expect(win.textContent).not.toMatch(/сила \(force\)/);
  expect(win.textContent).toContain("pu-mop-1");
  expect(win.textContent).toContain("hyper");
  expect(win.textContent).toContain("busy: #12");
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "recycle", params: {} }));
});

// Назад ничего не очищает: выбранный режим, проект и модель живут.
test("#522 back keeps the chosen mode and origin", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /Переключить на другой проект/i }));
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  fireEvent.change(screen.getByLabelText(/новый проект/i),
    { target: { value: "https://x/y/other.git" } });
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  fireEvent.click(screen.getByRole("button", { name: /^Назад$/ }));
  // финал -> параметры: адрес на месте
  expect(screen.getByLabelText(/новый проект/i)).toHaveValue("https://x/y/other.git");
  fireEvent.click(screen.getByRole("button", { name: /^Назад$/ }));
  // параметры -> режим: выбор на месте, повторный проход не с нуля
  expect((screen.getByRole("radio", { name: /Переключить на другой проект/i }) as HTMLInputElement)
    .checked).toBe(true);
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

// Финальный экран = подтверждение (#527, канон): заголовок «Проверьте
// параметры операции», факты Папет/Узел/Текущий статус/Операция,
// статус -- цитата строки ростера, у переключения -- «Новый проект».
test("#527 the final screen reads the ticket's confirmation wording", () => {
  vi.stubGlobal("fetch", vi.fn());
  // состояние-цитата: free (master) -- факт цели, не очеловеченное слово
  const { onOrder } = mount({ ...ROW, state: "free (master)" });
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /Переключить на другой проект/i }));
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  fireEvent.change(screen.getByLabelText(/новый проект/i),
    { target: { value: "https://x/y/other.git" } });
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  const win = screen.getByTestId("control-window");
  expect(within(win).getByText("Проверьте параметры операции")).toBeInTheDocument();
  // факты канона: имя, узел, статус-цитата, режим
  expect(win.textContent).toMatch(/Папет: pu-mop-1/);
  expect(win.textContent).toMatch(/Узел: hyper/);
  expect(win.textContent).toMatch(/Текущий статус: free \(master\)/);
  expect(win.textContent).toMatch(/Операция: Переключить на другой проект/);
  expect(win.textContent).toMatch(/Новый проект: https:\/\/x\/y\/other\.git/);
  // «Внимание:» -- у необратимого режима, буллеты канона
  expect(win.textContent).toMatch(/Внимание:/);
  expect(win.textContent).toMatch(/Контекст диалога будет потерян/);
  // «частичный исход показывается как есть…» упрощена (#527)
  expect(win.textContent).not.toMatch(/частичный исход показывается как есть/);
  expect(win.textContent).not.toMatch(/восстановления не обещается/);
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "update", params: { fresh: true, origin: "https://x/y/other.git" } }));
});

// Предупреждение о необратимости -- только у необратимых операций
// (#527): keep обратим, у него нет ни «Внимание:», ни буллетов потерь.
test("#527 reversibility is named only where it is lost", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /Продолжить работу/i }));
  fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/Операция: Продолжить работу/);
  expect(win.textContent).not.toMatch(/Внимание:/);
  expect(win.textContent).not.toMatch(/будут потеряны/);
});

// Фиксированная высота (#527) -- структурно: контейнер шага держит
// minHeight по максимуму содержания шагов (последствия самого длинного
// набора строк), чтобы смена режима/шага не прыгала высотой. jsdom
// layout не считает -- проверяем инвариант: на первом шаге потока
// обновления НЕТ динамических полей, и minHeight стоит на контейнере.
test("#527 the step body reserves the tallest step content, no fields appear on the mode step", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
  const win = screen.getByTestId("control-window");
  const body = within(win).getByTestId("wizard-body");
  // резерв виден как инлайн-стиль minHeight (структурная константа, не
  // jsdom-цифра): положительное число px
  const mh = body.style.minHeight;
  expect(mh).toMatch(/^\d+(\.\d+)?px$/);
  // динамических полей на шаге выбора режима нет: поле репозитория
  // живёт на шаге параметров, не появляется на первом
  expect(within(body).queryByRole("textbox")).toBeNull();
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
    <ControlWindow opened row={{ ...ROW, name: "pu-mop-2" }} session={ADMIN} admits={ALL}
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
