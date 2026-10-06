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
const card = () => renderUi(<PuppetCard session={session}
  target={row.name} onClose={() => {}} />);

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
  const button = within(actions).getByRole("button", { name: "экран" });
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
    .toEqual(["экран", "правка"]);
  expect(within(actions).queryByRole("button", { name: "сообщение" })).toBeNull();
});

// Вход #516 -> #520: клик «правка» в строке -> визард -> действие. Окно
// переключает шаги и закрывается исполнением (#520), вердикты
// каждой операции показываются её собственным механизмом (#487: рестарт
// и обновление -- OperationResult, #488: модель -- балун, #516:
// разрушительные -- OperationResult).
test.each(["рестарт", "правка", "модель"])("#487 %s result never widens the row", async (action) => {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => response(
    init?.method === "POST" ? { id: "op-1", status: "accepted" }
      : url.endsWith("/models") ? { models: ["other-model"] }
      : { id: "op-1", status: "succeeded" })));
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  fireEvent.click(within(actions).getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  // #520: пункт update зовётся «обновление», дубля имени больше нет
  const item = within(win).getByRole("button",
    { name: action === "правка" ? "обновление" : action });
  fireEvent.click(item);
  // #513: обратимые шлют сразу; у обновления/модели -- шаг параметров
  if (action === "правка") {
    fireEvent.click(screen.getByRole("radio", { name: "продолжить как было" }));
    fireEvent.click(screen.getByRole("button", { name: /^выполнить$/i }));
  } else if (action === "модель") {
    fireEvent.click(await screen.findByRole("radio", { name: "other-model" }));
    fireEvent.click(screen.getByRole("button", { name: /^выполнить$/i }));
  }
  // #487: вердикт строку не расширяет. #488: у модели вердикт уехал в
  // балун (слово оператора); рестарт и обновление показывают его
  // модальным диалогом. Инвариант один -- строки действий чисты.
  if (action === "модель") {
    await waitFor(() => expect(actions).not.toHaveTextContent("перезапущен"));
  } else {
    // слово вердикта у каждого своё: рестарт -- «перезапущен»,
    // обновление -- «обновлено» (#520)
    const result = await screen.findByText(
      action === "правка" ? "обновлено" : "перезапущен");
    expect(result.closest('[role="dialog"]')).not.toBeNull();
    expect(actions).not.toHaveTextContent("обновлено");
    expect(actions).not.toHaveTextContent("перезапущен");
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
  fireEvent.click(screen.getByRole("button", { name: "обновить" }));
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
  fireEvent.click(screen.getByRole("button", { name: "увеличить" }));
  expect(terminal).toHaveStyle({ transform: `scale(${initial + 0.25})` });
  fireEvent.click(screen.getByRole("button", { name: "уменьшить" }));
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

test.each(["рестарт", "правка", "модель"])("#487 %s preserves a result completed while its dialog was closed", async (action) => {
  let finish!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  const mock = vi.fn(async (url: string, init?: RequestInit) => init?.method === "POST"
    ? response({ id: "op-1", status: "accepted" })
    : url.endsWith("/models") ? response({ models: ["other-model"] }) : pending);
  vi.stubGlobal("fetch", mock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  // вход #520: строка -> визард -> действие; окно живёт до исполнения
  fireEvent.click(within(actions).getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button",
    { name: action === "правка" ? "обновление" : action }));
  if (action === "правка") {
    fireEvent.click(screen.getByRole("radio", { name: "продолжить как было" }));
    fireEvent.click(screen.getByRole("button", { name: /^выполнить$/i }));
  } else if (action === "модель") {
    fireEvent.click(await screen.findByRole("radio", { name: "other-model" }));
    fireEvent.click(screen.getByRole("button", { name: /^выполнить$/i }));
  }
  // #513: подтверждения у обратимых нет -- POST уже ушёл
  if (action === "модель") {
    // #488: вердикт модели -- балун; ход живёт в useAdminOp, повторная
    // команда открывает форму, дедуп на сервере (id на месте).
    await waitFor(() => expect(mock.mock.calls
      .filter((c) => c[1]?.method === "POST")).toHaveLength(1));
    fireEvent.click(within(actions).getByRole("button", { name: "правка" }));
    const again = await screen.findByTestId("control-window");
    fireEvent.click(within(again).getByRole("button", { name: "модель" }));
    expect(await screen.findByTestId("model-choices")).toBeInTheDocument();
  } else {
    await screen.findByText("принято");
    fireEvent.click(screen.getByRole("button", { name: "закрыть" }));
    await act(async () => { finish(response({ status: "failed", error: "review refusal" })); });
    fireEvent.click(within(actions).getByRole("button", { name: "правка" }));
    const again = await screen.findByTestId("control-window");
    fireEvent.click(within(again).getByRole("button",
      { name: action === "правка" ? "обновление" : action }));
    expect(await screen.findByText(/review refusal/)).toBeInTheDocument();
    expect(mock.mock.calls.filter((c) => c[1]?.method === "POST")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "новая команда" }));
    expect(screen.queryByText(/review refusal/)).toBeNull();
    // #513: рестарт шлёт POST сразу; у обновления «новая команда»
    // открывает визард заново (повторный выбор режима) -- у каждой свой
    // честный вход
    if (action === "рестарт") {
      await waitFor(() => expect(mock.mock.calls
        .filter((c) => c[1]?.method === "POST")).toHaveLength(2));
    } else {
      expect(await screen.findByTestId("update-modes")).toBeInTheDocument();
    }
  }
});

test("#512 the row offers the destructive actions, a non-admin never sees them", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(screen.getByRole("button", { name: "правка" }));
  const win = screen.getByTestId("control-window");
  // #512/#516/#520: три разрушительных действия -- пунктами визарда
  // правки цели, каждое -- единый экран последствий с одним
  // подтверждением
  for (const label of ["recycle", "delete", "wipe"]) {
    expect(within(win).getByRole("button", { name: label })).toBeInTheDocument();
  }
  // не-админ всей колонки действий не видит -- окна и разрушительных
  // тоже; первый стол убираем до второго рендера, иначе он отвечает за
  // кнопки
  cleanup();
  renderUi(<PoolTable projects={snapshot.projects} session={{ login: "user", role: "user" }} />);
  expect(screen.queryByRole("button", { name: "правка" })).toBeNull();
  expect(screen.queryByRole("button", { name: "recycle" })).toBeNull();
  expect(screen.queryByRole("button", { name: "delete" })).toBeNull();
  expect(screen.queryByRole("button", { name: "wipe" })).toBeNull();
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
    .getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  expect(win.textContent).toContain(row.name);
  for (const label of ["обновление", "модель", "рестарт", "recycle", "delete", "wipe"]) {
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
  fireEvent.click(screen.getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  const item = within(win).getByRole("button", { name: new RegExp(reason) });
  expect(item).toBeDisabled();
  expect(item).toHaveAttribute("aria-label", reason);
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

// Разрушительный пункт (#516 -> #520): единый экран последствий -- что
// гибнет и факты точной цели вместе, одна «подтвердить» (прежде --
// preview и подтверждение двумя модами #512).
test("#516 a destructive item rides its two-step flow from the window", async () => {
  const fetchMock = vi.fn(async () => response({ id: "op-1", status: "accepted" }));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(within(screen.getByRole("group", { name: `действия ${row.name}` }))
    .getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "recycle" }));
  // окно живо (#520): единый экран последствий с фактами цели
  expect(screen.getByTestId("control-window")).toBeInTheDocument();
  expect(await screen.findByText(/несохранённая работа погибнет/i)).toBeInTheDocument();
  expect(screen.getByTestId("control-window").textContent).toContain(row.name);
  expect(screen.queryByRole("button", { name: /продолжить/i })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^подтвердить$/ }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops",
    expect.objectContaining({ method: "POST" })));
});

// Вердикт разрушительной операции (#516): инлайн-строки рядом с кнопками
// больше нет (кнопки уехали в окно) -- вердикт показывает OperationResult,
// единый механизм с рестартом/правкой (#465/#473).
test("#516 a destructive verdict rides the OperationResult modal", async () => {
  const fetchMock = vi.fn(async (_url: string | URL, init?: RequestInit) =>
    response(init?.method === "POST" ? { id: "op-1", status: "accepted" }
      : { id: "op-1", action: "recycle", target: row.name, status: "failed",
          error: "image builder refused" }));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(within(screen.getByRole("group", { name: `действия ${row.name}` }))
    .getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "recycle" }));
  expect(await screen.findByText(/несохранённая работа погибнет/i)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^подтвердить$/ }));
  const verdict = await screen.findByText(/не удался/i);
  // вердикт -- в модале операции, не строкой рядом с кнопками
  expect(verdict.closest('[role="dialog"]')).not.toBeNull();
  expect(verdict).toHaveTextContent("image builder refused");
});

// Обратимый пункт шлёт сразу (#513 сохраняется в визарде #520): рестарт
// с первого экрана -- POST сразу, Stepper не переключается.
test("#516 the restart item posts at once, reversible #513 kept", async () => {
  const fetchMock = vi.fn(async () => response({ id: "op-1", status: "accepted" }));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PoolTable projects={snapshot.projects} session={session} />);
  fireEvent.click(within(screen.getByRole("group", { name: `действия ${row.name}` }))
    .getByRole("button", { name: "правка" }));
  const win = await screen.findByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "рестарт" }));
  expect(screen.queryByTestId("control-window")).toBeNull();
  expect(screen.queryByText(/подтвердите рестарт/i)).toBeNull();
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops",
    expect.objectContaining({ method: "POST" })));
});
