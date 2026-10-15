// HYPOTHESIS: колонка растягивается и выдаёт результаты в строку; кнопки
// просмотра отсутствуют, масштаб короткого вывода не меняется, скролл общий.
// SOLUTION: компактные правые иконки, отдельные auto-load просмотрщики,
// неподвижная панель и собственная область прокрутки с реальным zoom.
// RESULT: проверены resize по высоте и результат, пришедший в закрытый диалог.
// STATUS: FIXED — see #487
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PoolTable } from "./PoolTable";
import { PuppetCard } from "./PuppetCard";
import { emptySnapshot, renderUi } from "../test-utils";
import type { Puppet } from "../types";

// #512: файл без cleanup держал DOM между проверками -- ростер из ранней
// проверки оставался на экране поздней, и screen-запросы читали чужие
// строки. Монтирование разрушительных (#512) проверяется по этому же
// экрану, и без cleanup подсчёт кнопок считал бы чужие.
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const session = { login: "admin", role: "admin" as const };
const row: Puppet = {
  name: "pu-mop-1", node: "node", alloc_status: "running", state: "free (master)",
  kind: "free", bucket: "free", detail: null, branch: "master", owner: "",
  llm: "model", origin: "git@example.test:g/mop.git", disk_kb: null,
};
const snapshot = emptySnapshot({ projects: [{ name: "mop", puppets: [row],
  counts: { puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0 } }] });
const response = (body: unknown) => ({ ok: true, json: async () => body }) as Response;
// #661: zoom и refresh остались у startup-снимка, не у живого терминала.
const card = () => {
  const fetchTail = vi.mocked(fetch);
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => String(url).includes('/history')
    ? Promise.resolve({ ok: true, status: 200, json: async () => ({ epoch: 'e', session_id: 's', cursor: 'c', records: [], reset: false, partial: false, has_more: false }) })
    : String(url).endsWith('/tail')
      ? fetchTail(url, init).then(async result => result.ok
        ? response({ ...await result.json(), source: 'initialization' }) : result)
      : String(url).includes('/read?') ? new Promise<Response>(() => {})
      : Promise.resolve(response({ ok: true }))));
  const view = renderUi(<PuppetCard session={session} target={row.name} onClose={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  return view;
};

const originalScrollTop = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTop");
// второй afterEach -- тому же «каждому разу»: scrollTop-дескриптор (#487)
// возвращается после каждого стола, не только конца файла
afterEach(() => {
  if (originalScrollTop) Object.defineProperty(HTMLElement.prototype, "scrollTop", originalScrollTop);
  else Reflect.deleteProperty(HTMLElement.prototype, "scrollTop");
});

// #507: иконка «лог» ушла. #516: в строке только «экран» и «правка»,
// все операции цели -- пунктами в окне правки.
test("row icon экран opens only its own dialog", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response({ models: [], lines: [] })));
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  const button = within(actions).getByRole("button", { name: "Экран" });
  expect(button.querySelector("svg")).not.toBeNull();
  expect(button.textContent).toBe("");
  fireEvent.click(button);
  const dialog = await screen.findByRole("dialog", { name: `экран ${row.name}` });
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(dialog).toHaveTextContent(row.name);
});

test("read-only viewers have no action icons", () => {
  renderUi(<PoolTable projects={snapshot.projects} session={{ login: "user", role: "user" }} />);
  expect(screen.queryByRole("group", { name: /действия/ })).toBeNull();
});

test("#487 actions have a right-aligned shrink-to-fit column", () => {
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  expect(screen.getByRole("columnheader", { name: "действия" })).toHaveStyle({ textAlign: "right", width: "1%" });
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  expect(actions).toHaveStyle({ justifyContent: "flex-end", flexWrap: "nowrap" });
  expect(actions.closest("td")).toHaveStyle({ width: "1%", whiteSpace: "nowrap", textAlign: "right" });
  // #516: строка -- экран и правка; модель/рестарт/разрушительные уехали
  // пунктами в окно правки, состав перечислен, не только число
  expect(within(actions).getAllByRole("button")
    .map((b) => b.getAttribute("aria-label") ?? b.textContent?.trim()))
    .toEqual(["Экран", "Правка"]);
  expect(within(actions).queryByRole("button", { name: "сообщение" })).toBeNull();
});

// Вход #516 -> #522: клик «правка» в строке -> визард -> действие.
// Каждая операция заканчивается финальным экраном «что произойдёт»,
// «Завершить» = согласие (#522); после него визард показывает экран
// хода, вердикт -- экраном визарда (#487: строка чиста), модель --
// балун (#488), визард закрывается сразу.
test.each(["Рестарт", "Правка", "Модель"])("#487 %s result never widens the row", async (action) => {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => response(
    init?.method === "POST" ? { id: "op-1", status: "accepted" }
      : url.endsWith("/models") ? { models: ["other-model"] }
      : { id: "op-1", status: "succeeded" })));
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  fireEvent.click(within(actions).getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  const item = within(win).getByRole("button",
    { name: action === "Правка" ? "Обновление" : action });
  fireEvent.click(item);
  // #522: финальный экран у всех; выбор режима/модели ведёт на него,
  // «Завершить» шлёт приказ
  if (action === "Правка") {
    fireEvent.click(screen.getByRole("radio", { name: "Продолжить работу" }));
    fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  } else if (action === "Модель") {
    fireEvent.click(await screen.findByRole("radio", { name: "other-model" }));
    fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  }
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  // #487: вердикт строку не расширяет. #522: у рестарта и обновления
  // ход и вердикт -- экран визарда; #488: у модели вердикт -- балун,
  // визард закрылся сразу. Инвариант один -- строки действий чисты.
  if (action === "Модель") {
    await waitFor(() => expect(screen.queryByTestId("control-window")).toBeNull());
    await waitFor(() => expect(actions).not.toHaveTextContent("Перезапущен"));
  } else {
    // экран хода визарда жив: слово статуса и Loader внутри окна
    const win = screen.getByTestId("control-window");
    await waitFor(() => expect(within(win).getByRole("status")).toBeInTheDocument());
    expect(actions).not.toHaveTextContent("Обновлено");
    expect(actions).not.toHaveTextContent("Перезапущен");
  }
});

// #507: карточка открывает один источник -- экран.
test("#487 the screen shows loading on open", async () => {
  let release!: (r: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { release = resolve; })));
  card();
  expect(screen.getByRole("status")).toBeInTheDocument();
  expect(vi.mocked(fetch)).toHaveBeenCalledWith("/api/puppet/pu-mop-1/tail", expect.anything());
  await act(async () => { release(response({ lines: ["loaded"] })); });
  expect(await screen.findByText("loaded")).toBeInTheDocument();
  expect(screen.queryByRole("status")).toBeNull();
});

test("#489 refresh preserves old output until the new frame arrives", async () => {
  let release!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { release = resolve; });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response({ lines: ["old screen"] })).mockReturnValueOnce(pending));
  card();
  await screen.findByText("old screen");
  fireEvent.click(screen.getByRole("button", { name: "Обновить" }));
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByText("old screen")).toBeInTheDocument();
  await act(async () => { release(response({ lines: [] })); });
  expect(await screen.findByText("вывод пуст")).toBeInTheDocument();
});

test.each([300, 1200])("#487 zoom changes even when %s-pixel text already fits", async (natural) => {
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(natural);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(400);
  vi.stubGlobal("fetch", vi.fn(async () => response({ lines: ["first", "", "last"] })));
  card();
  const terminal = await screen.findByTestId("tail-lines");
  const initial = Math.min(1, 600 / natural);
  expect(terminal).toHaveStyle({ whiteSpace: "pre", transform: `scale(${initial})` });
  expect(terminal.textContent).toBe("first\n\nlast");
  fireEvent.click(screen.getByRole("button", { name: "Увеличить" }));
  expect(terminal).toHaveStyle({ transform: `scale(${initial + 0.25})` });
  fireEvent.click(screen.getByRole("button", { name: "Уменьшить" }));
  expect(terminal).toHaveStyle({ transform: `scale(${initial})` });
  // #511: кнопки «по ширине» больше нет — сброс к автоподгонке был
  // нерабочим решением; zoom=null остаётся начальным состоянием, ручные
  // +/- остаются.
  expect(screen.queryByRole("button", { name: "по ширине" })).toBeNull();
  expect(screen.getByRole("toolbar", { name: "просмотр" }).closest('[data-testid="output-scroll"]')).toBeNull();
  expect(screen.getByTestId("output-scroll")).toHaveStyle({ overflow: "auto", minHeight: "0" });
});

test("#487 height-only resize keeps the last log line visible", async () => {
  let height = 300;
  let top = 0;
  let notify = () => {};
  vi.stubGlobal("ResizeObserver", class {
    constructor(private callback: () => void) {}
    observe(el: HTMLElement) { if (el.dataset.testid === "output-scroll") notify = this.callback; }
    unobserve() {}
    disconnect() {}
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => height);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(300);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(1200);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(1200);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "get").mockImplementation(() => top);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "set").mockImplementation((value) => { top = Math.min(value, 1200 - height); });
  vi.stubGlobal("fetch", vi.fn(async () => response({ lines: ["last log line"] })));
  card();
  await screen.findByText("last log line");
  expect(top).toBe(900);
  await act(async () => { height = 100; notify(); });
  expect(top).toBe(1100);
});

test.each(["Рестарт", "Правка", "Модель"])("#487 %s preserves a result completed while its dialog was closed", async (action) => {
  let finish!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  const mock = vi.fn(async (url: string, init?: RequestInit) => init?.method === "POST"
    ? response({ id: "op-1", status: "accepted" })
    : url.endsWith("/models") ? response({ models: ["other-model"] }) : pending);
  vi.stubGlobal("fetch", mock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  // вход #522: строка -> визард -> финальный экран -> «Завершить»
  fireEvent.click(within(actions).getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button",
    { name: action === "Правка" ? "Обновление" : action }));
  if (action === "Правка") {
    fireEvent.click(screen.getByRole("radio", { name: "Продолжить работу" }));
    fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  } else if (action === "Модель") {
    fireEvent.click(await screen.findByRole("radio", { name: "other-model" }));
    fireEvent.click(screen.getByRole("button", { name: /^Продолжить$/ }));
  }
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  if (action === "Модель") {
    // #488: вердикт модели -- балун; визард закрылся исполнением,
    // повторная команда открывает форму, дедуп на сервере (id на месте).
    await waitFor(() => expect(mock.mock.calls
      .filter((c) => c[1]?.method === "POST")).toHaveLength(1));
    fireEvent.click(within(actions).getByRole("button", { name: "Правка" }));
    const again = await screen.findByTestId("control-window");
    fireEvent.click(within(again).getByRole("button", { name: "Модель" }));
    expect(await screen.findByTestId("model-choices")).toBeInTheDocument();
  } else {
    // #522: экран хода живёт в визарде -- «Принято» на экране хода
    await screen.findByText("Принято");
    await act(async () => { finish(response({ status: "failed", error: "review refusal" })); });
    // терминал приехал в закрытое окно: повторный вход (#522) -- не форма,
    // а сразу экран вердикта: поднятый движком стейт открывает ход без
    // выбора пункта и без второго POST
    fireEvent.click(within(actions).getByRole("button", { name: "Правка" }));
    await screen.findByTestId("control-window");
    expect(await screen.findByText(/review refusal/)).toBeInTheDocument();
    expect(mock.mock.calls.filter((c) => c[1]?.method === "POST")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Новая команда" }));
    // новая команда -- с выбора; у рестарта/обновления свой честный
    // вход через финальный экран
    if (action === "Рестарт") {
      fireEvent.click(screen.getByRole("button", { name: "Рестарт" }));
      fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
      await waitFor(() => expect(mock.mock.calls
        .filter((c) => c[1]?.method === "POST")).toHaveLength(2));
    } else {
      fireEvent.click(screen.getByRole("button", { name: "Обновление" }));
      expect(await screen.findByRole("radio", { name: "Продолжить работу" }))
        .toBeInTheDocument();
    }
  }
});

test("#512 the row offers the destructive actions, a non-admin never sees them", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(screen.getByRole("button", { name: "Правка" }));
  const win = screen.getByTestId("control-window");
  // #512/#516/#520: три разрушительных действия -- пунктами визарда
  // правки цели, каждое -- единый экран последствий с одним
  // подтверждением
  for (const label of ["Пересоздать", "Удалить", "Сбросить"]) {
    expect(within(win).getByRole("button", { name: label })).toBeInTheDocument();
  }
  // не-админ всей колонки действий не видит -- окна и разрушительных
  // тоже; первый стол убираем до второго рендера, иначе он отвечает за
  // кнопки
  cleanup();
  renderUi(<PoolTable projects={snapshot.projects} session={{ login: "user", role: "user" }} />);
  expect(screen.queryByRole("button", { name: "Правка" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Пересоздать" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Удалить" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Сбросить" })).toBeNull();
});

test("#487 opening the screen scrolls its viewport to the end", async () => {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(1200);
  vi.stubGlobal("fetch", vi.fn(async () => response({ lines: ["oldest", "latest"] })));
  card();
  await screen.findByText("latest");
  expect(screen.getByTestId("output-scroll").scrollTop).toBe(1200);
});

// ── #516: окно правки -- окно управления целью ──────────────────────────

// Пункты -- все операции цели в одном окне (#516): обновление (update,
// #520: без дубля имени «правка»), модель, рестарт и три разрушительных.
// Названия -- слова GUI, recycle/delete/wipe -- имена действий сервера.
// #520: окно больше не закрывается выбором пункта -- оно визард: выбор
// переключает шаг, приказ уходит движку с последнего экрана.
test("#516 the edit window lists every operation as an item", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response({ models: [] })));
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(within(screen.getByRole("group", { name: `действия ${row.name}` }))
    .getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  expect(win.textContent).toContain(row.name);
  for (const label of ["Обновление", "Модель", "Рестарт", "Пересоздать", "Удалить", "Сбросить"]) {
    expect(within(win).getByRole("button", { name: label })).toBeInTheDocument();
  }
});

// Дизейбл-причины #508 переезжают в пункт рестарта окна (#516): признак
// строки виден до клика, отказ после клика был бы вторым ответом.
test.each([
  ["down", "рестарт недоступен: не запущен"],
  ["failing", "рестарт недоступен: задача падает"],
])("#516 the restart item carries the %s disable reason", async (kind, reason) => {
  vi.stubGlobal("fetch", vi.fn());
  const target: Puppet = kind === "down"
    ? { ...row, alloc_status: "stopped", bucket: "down", state: "stopped" }
    : { ...row, kind: "failing", state: "FAILED: exit 1", detail: "exit 1" };
  const shot = emptySnapshot({ projects: [{ name: "mop", puppets: [target],
    counts: { puppets: 1, free: 0, busy: 1, sick: 0, silent: 0, down: 0 } }] });
  renderUi(<PoolTable projects={shot.projects} session={session} />);
  fireEvent.click(screen.getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  const item = within(win).getByRole("button", { name: new RegExp(reason) });
  expect(item).toBeDisabled();
  expect(item).toHaveAttribute("aria-label", reason);
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

// Разрушительный пункт (#516 -> #522): выбор -- сразу финальный экран
// последствий, «Завершить» = согласие, POST уходит с него.
test("#516 a destructive item rides its two-step flow from the window", async () => {
  const fetchMock = vi.fn(async () => response({ id: "op-1", status: "accepted" }));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(within(screen.getByRole("group", { name: `действия ${row.name}` }))
    .getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "Пересоздать" }));
  // окно живо (#522): финальный экран последствий с фактами цели
  expect(screen.getByTestId("control-window")).toBeInTheDocument();
  expect(await screen.findByText(/несохранённая работа погибнет/)).toBeInTheDocument();
  expect(screen.getByTestId("control-window").textContent).toContain(row.name);
  expect(screen.queryByRole("button", { name: /продолжить/i })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops",
    expect.objectContaining({ method: "POST" })));
});

// Вердикт разрушительной операции (#516 -> #522): экран хода визарда --
// терминальное слово с причиной внутри окна, второй моды нет.
test("#516 a destructive verdict rides the wizard progress screen", async () => {
  const fetchMock = vi.fn(async (_url: string | URL, init?: RequestInit) =>
    response(init?.method === "POST" ? { id: "op-1", status: "accepted" }
      : { id: "op-1", action: "recycle", target: row.name, status: "failed",
          error: "image builder refused" }));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(within(screen.getByRole("group", { name: `действия ${row.name}` }))
    .getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "Пересоздать" }));
  expect(await screen.findByText(/несохранённая работа погибнет/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^Завершить$/ }));
  const verdict = await screen.findByText(/Не удался/);
  // вердикт -- экраном визарда, в его же моде, не строкой и не второй модой
  expect(verdict.closest('[data-testid="control-window"]')).not.toBeNull();
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(verdict).toHaveTextContent("image builder refused");
});

// Рестарт -- через финальный экран (#522: единое правило
// подтверждения, immediate-POST ушёл): POST только после «Завершить».
test("#516 the restart item posts from its final screen", async () => {
  const fetchMock = vi.fn(async () => response({ id: "op-1", status: "accepted" }));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(within(screen.getByRole("group", { name: `действия ${row.name}` }))
    .getByRole("button", { name: "Правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "Рестарт" }));
  // финальный экран рестарта: последствия названы до POST
  expect(win.textContent).toMatch(/Рабочая копия не трогается: несохранённая работа живёт/);
  expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(within(win).getByRole("button", { name: /^Завершить$/ }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops",
    expect.objectContaining({ method: "POST" })));
});
