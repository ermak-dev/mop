// Папеты по проектам (#298): секция на проект, колонки и корзины со старой страницы.
import { fireEvent, screen, within } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { vi } from "vitest";
import { roster } from "../roster";
import { expectHead, renderUi } from "../test-utils";
import { COLS, PoolTable, projectNote } from "./PoolTable";
import type { Project, Puppet } from "../types";
import type { Session } from "../useSession";

// #452: прежняя проверка секций осталась, ожидания -- по новому контракту:
// слово статуса по kind, ветка чипом, аллокации в таблице нет.
const PROJECTS: Project[] = [{
  name: "mop",
  counts: { puppets: 2, free: 1, busy: 1, sick: 0, silent: 0, down: 0 },
  puppets: [
    { name: "pu-mop-1", node: "hyper", alloc_status: "running", state: "free (master)",
      kind: "free", bucket: "free", detail: null, branch: "master",
      owner: "", llm: "claude", origin: "git@h:g/mop.git", disk_kb: 2 * 1048576 },
    { name: "pu-mop-2", node: "gpu", alloc_status: "running", state: "busy: #12",
      kind: "busy", bucket: "busy", detail: null, branch: "#12",
      owner: "ermak", llm: "glm", origin: "git@h:g/mop.git", disk_kb: null },
  ],
}];

// HYPOTHESIS: авторазмер таблицы отдаёт место длинному имени и сдвигает первые колонки.
// SOLUTION: фиксированные колонки; первые три занимают 376px + рамку 2px.
// RESULT: проверка упала без фиксированной раскладки, после colgroup все 24 прошли.
// STATUS: FIXED — see #666
test('#666 the first three columns fit the default roster, later columns remain available', () => {
  renderUi(<PoolTable projects={PROJECTS} />);
  const table = screen.getByRole('table');
  expect(table).toHaveStyle({ tableLayout: 'fixed' });
  const columns = [...table.querySelectorAll('col')];
  expect(columns).toHaveLength(Object.keys(COLS).length);
  expect(columns.slice(0, 3).map(col => col.style.width)).toEqual(['168px', '120px', '88px']);
  expectHead(Object.values(COLS));
  expect(within(screen.getAllByRole('row')[1]).getAllByRole('cell')).toHaveLength(8);
});

test("projectNote names the buckets in order", () => {
  expect(projectNote(PROJECTS[0].counts)).toBe("1 свободен, 1 занят");
  expect(projectNote({ puppets: 0, free: 0, busy: 0, sick: 0, silent: 0, down: 0 })).toBe("");
});

test("a section per project: one status word, the branch in its own column", () => {
  renderUi(<PoolTable projects={PROJECTS} />);
  expect(screen.getByRole("heading", { name: /mop/ })).toHaveTextContent("1 свободен, 1 занят");
  expectHead(Object.values(COLS));
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveAttribute("data-kind", "free");
  expect(within(rows[0]).getByText("2 GB")).toBeInTheDocument();
  expect(within(rows[0]).getByText("-")).toBeInTheDocument();
  expect(within(rows[0]).getByText("свободен")).toBeInTheDocument();
  expect(rows[1]).toHaveAttribute("data-kind", "busy");
  expect(within(rows[1]).getByText("ermak")).toBeInTheDocument();
  // Слово статуса -- по виду, ветка -- своей колонкой: state-строка в
  // ячейке не ищется.
  expect(within(rows[1]).getByText("занят")).toBeInTheDocument();
  expect(within(rows[1]).getByText("#12")).toBeInTheDocument();
});

test("#588 no roster entries: a plain note", () => {
  renderUi(<PoolTable projects={[]} />);
  expect(screen.getByText("No sessions")).toBeInTheDocument();
});

// #452: одна колонка «статус» -- слово по виду вердикта, цвет по корзине;
// ветка -- чипом рядом, подробность -- в тултипе, аллокации в таблице нет.
const RICH: Project[] = [{
  name: "mop",
  counts: { puppets: 2, free: 1, busy: 1, sick: 0, silent: 0, down: 0 },
  puppets: [
    { name: "pu-mop-1", node: "hyper", alloc_status: "running",
      state: "busy: feat/452", kind: "busy", bucket: "busy",
      detail: null, branch: "feat/452",
      owner: "ermak", llm: "glm", origin: "git@h:g/mop.git", disk_kb: null },
    { name: "pu-mop-2", node: "gpu", alloc_status: "running",
      state: "HUNG (not responding)", kind: "hung", bucket: "sick",
      detail: "not responding", branch: null,
      owner: "", llm: "-", origin: "git@h:g/mop.git", disk_kb: null },
  ],
}];

test("#452 one status column: the word by kind, the color by bucket", () => {
  renderUi(<PoolTable projects={RICH} />);
  // Колонки: аллокации больше нет, статус -- один.
  expectHead(Object.values(COLS));
  const rows = screen.getAllByRole("row").slice(1);
  expect(within(rows[0]).getByText("занят")).toBeInTheDocument();
  expect(within(rows[1]).getByText("завис")).toBeInTheDocument();
});

test("#452 the branch is a chip of its own, no state-string parsing", () => {
  renderUi(<PoolTable projects={RICH} />);
  const rows = screen.getAllByRole("row").slice(1);
  // Ветка -- из поля branch, а не разобранный «busy: feat/452».
  expect(within(rows[0]).getByText("feat/452")).toBeInTheDocument();
  // Колонка роли (#588) не меняет смысл ветки: прочерк остаётся именно
  // в её ячейке, а не берётся из соседнего пользователя или модели.
  const cells = within(rows[1]).getAllByRole("cell");
  const branch = cells[Object.keys(COLS).indexOf("branch")];
  expect(branch).toHaveTextContent("-");
  expect(within(branch).getByText("-")).toBeInTheDocument();
});

test("#478 the repository is the section header, not a column", () => {
  renderUi(<PoolTable projects={PROJECTS} />);
  // Колонки «репозиторий» больше нет.
  expect(screen.queryByRole("columnheader", { name: "репозиторий" })).not.toBeInTheDocument();
  // Репозиторий -- строкой под заголовком секции, один раз на секцию.
  const section = screen.getByRole("heading", { name: /mop/ }).parentElement!;
  expect(within(section).getAllByText("git@h:g/mop.git")).toHaveLength(1);
});

test("#452 the detail rides under the status, the alloc lives in the tooltip", () => {
  renderUi(<PoolTable projects={RICH} />);
  const rows = screen.getAllByRole("row").slice(1);
  // Подробность на месте слова, аллокация ушла из таблицы в подсказку.
  expect(within(rows[1]).getByText("not responding")).toBeInTheDocument();
});

// Переходный снимок (сервер до #452): ни вида, ни корзины, ни ветки --
// статус «нет данных», строка не рассыпается, цвета серые. Буквальный
// null в bucket/kind -- как он приходит из JSON старого снимка.
test("#452 a transition snapshot without the new fields still renders", () => {
  const old: Project[] = [{
    name: "mop",
    counts: { puppets: 1, free: 0, busy: 1, sick: 0, silent: 0, down: 0 },
    puppets: [{
      name: "pu-mop-1", node: "hyper", alloc_status: "pending",
      state: "-", kind: null, bucket: null, detail: null, branch: null,
      owner: "", llm: "-", origin: "git@h:g/mop.git", disk_kb: null } as unknown as Puppet],
  }];
  renderUi(<PoolTable projects={old} />);
  expect(screen.getByText("нет данных")).toBeInTheDocument();
});

// HYPOTHESIS: неизвестный queued рисуется как «нет данных» без объяснения.
// SOLUTION: единый verdict-словарь даёт ожидание, detail приходит от сервера.
// RESULT: проверка была красной на прежнем словаре, после правки зелёная.
// STATUS: FIXED — see #619
test("#619 a pending puppet shows placement wait and its server reason", () => {
  const queued: Project[] = [{
    name: "mop",
    counts: { puppets: 1, free: 0, busy: 0, sick: 0, silent: 0, down: 1 },
    puppets: [{ name: "pu-mop-9", node: "-", alloc_status: "pending",
      state: "queued: no free slots in the pool", kind: "queued", bucket: "down",
      detail: "no free slots in the pool", branch: null,
      owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null }],
  }];
  renderUi(<PoolTable projects={queued} />);
  expect(screen.getByText("ожидает размещения")).toBeInTheDocument();
  expect(screen.getByText("no free slots in the pool")).toBeInTheDocument();
  expect(screen.queryByText("нет данных")).not.toBeInTheDocument();
  expect(screen.queryByText("поднимается")).not.toBeInTheDocument();
});

// ── #509: карточки-вердикты ушли из таблицы ────────────────────────────
// Терминал операции возвращается человеку балуном кадра
// (useOperationBalloons, проверен своим файлом), строка цели чиста:
// слово вердикта в строке дублировало бы балун и расширяло колонку
// статуса. Пропа verdicts у PoolTable больше нет вовсе.
const ADMIN: Session = { login: "anton", role: "admin" };
const UNKNOWN: Project[] = [{
  name: "mop",
  counts: { puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0 },
  puppets: [{
    name: "pu-mop-1", node: "hyper", alloc_status: "running", state: "free (master)",
    kind: "free", bucket: "free", detail: null, branch: "master",
    owner: "", llm: "claude", origin: "git@h:g/mop.git", disk_kb: null }],
}];

// HYPOTHESIS: фильтр роли скрывает проекты, а открытие привязано только к имени.
// SOLUTION: фильтровать канонический ростер по проекту, открывать управляемую
// строку только администратору, исключая интерактивные элементы и выделение.
// RESULT: до правки 9 проверок красные; после правки 20 проверок зелёные.
// Дополнительно воспроизведён клик по имени после выделения текста;
// защита имени от выделения проверена отдельным красным прогоном.
// STATUS: FIXED — see #664
const LOCAL = {
  source: "local" as const, id: "local-only", project: "local-only", user: "anton",
  role: null, role_source: "legacy" as const, unmanaged: true as const,
  conversation: null, display: "local workspace", cwd: "/workspace",
  addresses: ["anton.local-1"], identified: true,
};

const managedRow = (name = "pu-mop-1") => screen.getByText(name).closest("tr")!;

test("#664 project options use canonical roster strings, including local-only projects", () => {
  const sessions = [...roster(PROJECTS).map((entry) => ({ ...entry, project: "canonical-project" })), LOCAL];
  renderUi(<PoolTable projects={PROJECTS} sessions={sessions} session={ADMIN} />);
  const filter = screen.getByRole("combobox", { name: "Проект" });
  expect(filter).toHaveValue("");
  expect(within(filter).getAllByRole("option").map((option) => [
    (option as HTMLOptionElement).value, option.textContent,
  ])).toEqual([["", "Все проекты"], ["canonical-project", "canonical-project"], ["local-only", "local-only"]]);
  expect(screen.queryByRole("combobox", { name: "Role" })).not.toBeInTheDocument();
  expect(screen.getAllByRole("columnheader", { name: "Role" })).toHaveLength(2);
  fireEvent.change(filter, { target: { value: "local-only" } });
  expect(screen.getByText("local workspace")).toBeInTheDocument();
  expect(screen.queryByText("pu-mop-1")).not.toBeInTheDocument();
  fireEvent.change(filter, { target: { value: "canonical-project" } });
  expect(screen.getByText("pu-mop-1")).toBeInTheDocument();
  expect(screen.queryByText("local workspace")).not.toBeInTheDocument();
});

test("#664 a selected project stays selected when snapshots remove its last entry", () => {
  const { rerender } = renderUi(<PoolTable projects={PROJECTS} sessions={[...roster(PROJECTS), LOCAL]} />);
  const filter = screen.getByRole("combobox", { name: "Проект" });
  fireEvent.change(filter, { target: { value: "local-only" } });
  rerender(<MantineProvider><PoolTable projects={PROJECTS} sessions={roster(PROJECTS)} /></MantineProvider>);
  expect(screen.getByRole("combobox", { name: "Проект" })).toHaveValue("local-only");
  expect(screen.getByRole("option", { name: "local-only" })).toHaveValue("local-only");
  expect(screen.queryByText("pu-mop-1")).not.toBeInTheDocument();
  expect(screen.getByText("No sessions match this project")).toBeInTheDocument();
});

test("#664 legacy local-only projects are filterable without canonical sessions", () => {
  renderUi(<PoolTable projects={PROJECTS} masters={[{
    project: "legacy-project", master: "anton.local-2", user: "anton",
    session: "legacy workspace", cwd: "/legacy", puppets: [],
  }]} />);
  fireEvent.change(screen.getByRole("combobox", { name: "Проект" }), { target: { value: "legacy-project" } });
  expect(screen.getByText("legacy workspace")).toBeInTheDocument();
  expect(screen.queryByText("pu-mop-1")).not.toBeInTheDocument();
});

test("#664 every noninteractive part of a managed row opens its name once", () => {
  const onOpen = vi.fn();
  renderUi(<PoolTable projects={PROJECTS} session={ADMIN} onOpen={onOpen} selected="pu-mop-1" />);
  const row = managedRow();
  const cells = within(row).getAllByRole("cell");
  for (const target of [within(row).getByText("hyper"), within(row).getByText("свободен"), cells[5], row]) {
    onOpen.mockClear();
    fireEvent.click(target);
    expect(onOpen).toHaveBeenCalledExactlyOnceWith("pu-mop-1");
  }
  expect(row).toHaveAttribute("aria-selected", "true");
  expect(managedRow("pu-mop-2")).toHaveAttribute("aria-selected", "false");
  expect(row).toHaveAttribute("data-selected", "true");
  expectHead(Object.values(COLS));
  expect(screen.queryByRole("columnheader", { name: "действия" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Restart|Delete|Escape/ })).not.toBeInTheDocument();
});

test("#664 Enter and Space on a focused row open, other keys do not", () => {
  const onOpen = vi.fn();
  renderUi(<PoolTable projects={PROJECTS} session={ADMIN} onOpen={onOpen} />);
  const row = managedRow();
  expect(row).toHaveAttribute("tabindex", "0");
  row.focus();
  expect(row).toHaveFocus();
  fireEvent.keyDown(row, { key: "Enter" });
  fireEvent.keyDown(row, { key: " " });
  fireEvent.keyDown(row, { key: "ArrowDown" });
  expect(onOpen.mock.calls).toEqual([["pu-mop-1"], ["pu-mop-1"]]);
});

test("#664 interactive descendants and selected text do not bubble into row opening", () => {
  const onOpen = vi.fn();
  renderUi(<PoolTable projects={PROJECTS} session={ADMIN} onOpen={onOpen} />);
  const row = managedRow();
  const name = within(row).getByRole("button", { name: "pu-mop-1" });
  fireEvent.click(name);
  expect(onOpen).toHaveBeenCalledExactlyOnceWith("pu-mop-1");
  onOpen.mockClear();
  fireEvent.keyDown(name, { key: "Enter" });
  fireEvent.keyDown(name, { key: " " });
  // Делегирование не должно зависеть от нынешнего содержимого ячеек.
  for (const tag of ["button", "a", "input", "select", "textarea"] as const) {
    const element = document.createElement(tag);
    if (tag === "a") element.setAttribute("href", "#details");
    row.cells[2].append(element);
    fireEvent.click(element);
    fireEvent.keyDown(element, { key: "Enter" });
    fireEvent.keyDown(element, { key: " " });
    element.remove();
  }
  expect(onOpen).not.toHaveBeenCalled();
  const text = within(row).getByText("hyper");
  const range = document.createRange();
  range.selectNodeContents(text);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  expect(window.getSelection()!.toString()).toBe("hyper");
  try {
    fireEvent.click(text, { detail: 1 });
    fireEvent.click(name, { detail: 1 });
    expect(onOpen).not.toHaveBeenCalled();
  } finally {
    window.getSelection()!.removeAllRanges();
  }
});

test('#664 keyboard activation ignores unrelated text selection', () => {
  const onOpen = vi.fn();
  renderUi(<><p>selected conversation text</p><PoolTable projects={PROJECTS} session={ADMIN} onOpen={onOpen} /></>);
  const range = document.createRange();
  range.selectNodeContents(screen.getByText('selected conversation text'));
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  try {
    fireEvent.click(within(managedRow()).getByRole('button', { name: 'pu-mop-1' }), { detail: 0 });
    expect(onOpen).toHaveBeenCalledExactlyOnceWith('pu-mop-1');
  } finally { window.getSelection()!.removeAllRanges(); }
});

test.each([undefined, { login: null, role: null }, { login: 'reader', role: 'user' } as Session,
  { login: "reader", role: "guest" } as unknown as Session, { login: "operator", role: "operator" } as unknown as Session])(
  "#664 nonadmin viewer %j cannot open even when given a callback", (session) => {
    const onOpen = vi.fn();
    renderUi(<PoolTable projects={PROJECTS} session={session} onOpen={onOpen} />);
    const row = managedRow();
    fireEvent.click(row);
    fireEvent.click(within(row).getByText("pu-mop-1"));
    fireEvent.keyDown(row, { key: "Enter" });
    fireEvent.keyDown(row, { key: " " });
    expect(onOpen).not.toHaveBeenCalled();
    expect(row).not.toHaveAttribute("tabindex");
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  },
);

test("#664 legacy local rows never open or gain action cells for admins", () => {
  const onOpen = vi.fn();
  renderUi(<PoolTable projects={[]} sessions={[LOCAL]} session={ADMIN} onOpen={onOpen} />);
  const row = screen.getByText("local workspace").closest("tr")!;
  fireEvent.click(row);
  fireEvent.click(within(row).getByText("local workspace"));
  fireEvent.keyDown(row, { key: "Enter" });
  fireEvent.keyDown(row, { key: " " });
  expect(onOpen).not.toHaveBeenCalled();
  expect(row).not.toHaveAttribute("tabindex");
  expect(within(row).getAllByRole("cell")).toHaveLength(Object.keys(COLS).length);
  expectHead(Object.values(COLS));
});

test("#509 the row draws no verdict card: terminals ride balloons now", () => {
  renderUi(<PoolTable projects={UNKNOWN} session={ADMIN} />);
  expect(screen.queryByTestId("target-verdict-pu-mop-1")).toBeNull();
  // проп verdicts больше не существует: директива ниже ругается именно на
  // него; вернёт кто проп -- директива станет лишней и tsc покраснеет
  renderUi(<PoolTable projects={UNKNOWN} session={ADMIN}
    // @ts-expect-error #509: пропа verdicts у PoolTable больше нет
    verdicts={{ "pu-mop-1": { status: "unknown" } }} />);
  expect(screen.queryByTestId("target-verdict-pu-mop-1")).toBeNull();
});
