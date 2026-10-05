// Папеты по проектам (#298): секция на проект, колонки и корзины со старой страницы.
import { screen, within } from "@testing-library/react";
import { expectHead, renderUi } from "../test-utils";
import { COLS, PoolTable, projectNote } from "./PoolTable";
import type { Project, Puppet } from "../types";

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

test("no projects: a plain note", () => {
  renderUi(<PoolTable projects={[]} />);
  expect(screen.getByText("папетов нет")).toBeInTheDocument();
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
  // Нет ветки -- прочерк в ячейке ветки (четвёртая колонка): слово не
  // изображает ветку. Прочерк в ней один: owner «-» и llm «-» в этой
  // строке -- другие колонки, у каждой свой.
  const cells = within(rows[1]).getAllByRole("cell");
  expect(cells[3]).toHaveTextContent("-");
  expect(within(cells[3]).getByText("-")).toBeInTheDocument();
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

// ── #504: unknown-карточка цели из SSE-канала вердиктов ────────────────
// HYPOTHESIS: операция с исходом unknown молчала -- вердикт жил только в
// памяти кнопки, строка цели о нём не знала; повторить операцию человек
// мог, не зная, что прежняя ещё неизвестна. SOLUTION: строка цели с
// терминальным вердиктом канала (#504) рисует карточку исхода; unknown --
// жёлтым со ссылкой в журнал, автоповтора на unknown нет (#476).
// RESULT: тесты написаны до реализации (RED).
import type { TargetVerdict } from "../useOperationFeed";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

const UNKNOWN: Project[] = [{
  name: "mop",
  counts: { puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0 },
  puppets: [{
    name: "pu-mop-1", node: "hyper", alloc_status: "running", state: "free (master)",
    kind: "free", bucket: "free", detail: null, branch: "master",
    owner: "", llm: "claude", origin: "git@h:g/mop.git", disk_kb: null }],
}];

const v = (status: string, extra: Partial<TargetVerdict> = {}): TargetVerdict =>
  ({ status, id: "op-1", error: null, lostVerdict: false, action: "restart", ...extra });

test("#504 an unknown verdict draws the target card with a journal link", () => {
  renderUi(<PoolTable projects={UNKNOWN} session={ADMIN}
                      verdicts={{ "pu-mop-1": v("unknown") }} />);
  // Карточка на строке цели: слово канала, ссылка в журнал.
  const card = screen.getByTestId("target-verdict-pu-mop-1");
  expect(card).toHaveTextContent("исход неизвестен");
  expect(card).toHaveTextContent("смотри журнал");
  const link = within(card).getByRole("link", { name: /журнал/i });
  expect(link).toHaveAttribute("href", "#journal");
});

test("#504 the card names a lost verdict, not a server refusal", () => {
  renderUi(<PoolTable projects={UNKNOWN} session={ADMIN}
                      verdicts={{ "pu-mop-1": v("unknown", { lostVerdict: true }) }} />);
  const card = screen.getByTestId("target-verdict-pu-mop-1");
  expect(card).toHaveTextContent("исход неизвестен");
  // Потерянный вердикт -- не отказ сервера: фразы отказа на карточке нет.
  expect(card).not.toHaveTextContent("отказано");
});

test("#504 no retry on unknown: the card carries no operation button", () => {
  renderUi(<PoolTable projects={UNKNOWN} session={ADMIN}
                      verdicts={{ "pu-mop-1": v("unknown") }} />);
  const card = screen.getByTestId("target-verdict-pu-mop-1");
  expect(within(card).queryByRole("button")).toBeNull();
});

test("#504 a settled verdict draws its own word, not the unknown card", () => {
  renderUi(<PoolTable projects={UNKNOWN} session={ADMIN}
                      verdicts={{ "pu-mop-1": v("succeeded") }} />);
  const card = screen.getByTestId("target-verdict-pu-mop-1");
  expect(card).toHaveTextContent("выполнено");
  expect(card).not.toHaveTextContent("исход неизвестен");
});

test("#504 a target without a verdict draws no card", () => {
  renderUi(<PoolTable projects={UNKNOWN} session={ADMIN} verdicts={{}} />);
  expect(screen.queryByTestId("target-verdict-pu-mop-1")).toBeNull();
});

test("#504 the card follows the row, not the page: another target stays clean", () => {
  const two: Project[] = [{
    name: "mop",
    counts: { puppets: 2, free: 2, busy: 0, sick: 0, silent: 0, down: 0 },
    puppets: [UNKNOWN[0].puppets[0],
      { ...UNKNOWN[0].puppets[0], name: "pu-mop-2", node: "gpu" }],
  }];
  renderUi(<PoolTable projects={two} session={ADMIN}
                      verdicts={{ "pu-mop-1": v("unknown") }} />);
  expect(screen.getByTestId("target-verdict-pu-mop-1")).toBeInTheDocument();
  expect(screen.queryByTestId("target-verdict-pu-mop-2")).toBeNull();
});
