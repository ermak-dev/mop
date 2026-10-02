// Живые мастера (#305): строка -- живая Claude-сессия (#421/#423): проект,
// логин, сессия, каталог, адреса для send и папеты проекта, чья аренда на
// логине. Одна сессия отвечает с нескольких адресов (личный инбокс и общий
// проекта) -- адреса списком в одной строке; бэкенд группирует по sessionId,
// публично его не отдавая, UI свою identity не выдумывает. identified --
// признак серверного маркера: true -- строка склеена по сессии, false --
// legacy endpoint без личности (за ним может стоять сколько угодно сессий).
import { screen, within } from "@testing-library/react";
import { expectHead, renderUi } from "../test-utils";
import { COLS, EMPTY, Masters } from "./Masters";
import type { Master } from "../types";

const M: Master[] = [
  { project: "mop", master: "ermak.mate-7", addresses: ["ermak.mate-7", "ermak.mate-7-all"],
    identified: true, user: "ermak", session: "mop-ab", cwd: "/home/ermak/mop",
    puppets: ["pu-mop-1", "pu-mop-3"] },
  { project: "mop", master: "ivan.box-3", addresses: ["ivan.box-3"], identified: false,
    user: "ivan", session: "mop-cd", cwd: "-", puppets: [] },
];

test("a row per live session with all of its addresses", () => {
  renderUi(<Masters masters={M} />);
  expectHead(COLS);
  const ab = screen.getByText("mop-ab").closest("tr")!;
  expect(within(ab).getByText("ermak.mate-7")).toBeInTheDocument();
  expect(within(ab).getByText("ermak.mate-7-all")).toBeInTheDocument();
  expect(screen.getByText("pu-mop-1, pu-mop-3")).toBeInTheDocument();
});

// Число не врёт: сессии и legacy-адреса считаются раздельно.
test("#423 the header counts sessions and legacy endpoints apart", () => {
  renderUi(<Masters masters={M} />);
  expect(screen.getByText(/1 сессия, 1 по адресам/)).toBeInTheDocument();
});

// Legacy-строка честно помечена: endpoint, а не сессия.
test("#423 a legacy row says so instead of posing as a session", () => {
  renderUi(<Masters masters={M} />);
  const row = screen.getByText("mop-cd").closest("tr")!;
  expect(within(row).getByText(/MCP-адрес, сессия не подтверждена/)).toBeInTheDocument();
});

test("#423 same project, same session name, different addresses: two rows", () => {
  // Один проект: разлить строки по проекту нельзя -- различает адрес.
  const same: Master[] = [
    { project: "mop", master: "ermak.mate-7", addresses: ["ermak.mate-7"], identified: true,
      user: "ermak", session: "mop-ab", cwd: "-", puppets: [] },
    { project: "mop", master: "ermak.box-9", addresses: ["ermak.box-9"], identified: true,
      user: "ermak", session: "mop-ab", cwd: "-", puppets: [] },
  ];
  renderUi(<Masters masters={same} />);
  expect(screen.getAllByText("mop-ab").length).toBe(2);
  expect(screen.getByText("ermak.mate-7")).toBeInTheDocument();
  expect(screen.getByText("ermak.box-9")).toBeInTheDocument();
});

// #423, ревью: у двух сессий одного владельца бэкенд кладёт в обе строки
// один и тот же список папетов логина. Повторять его как собственность
// каждой сессии -- исходный дефект #421; список -- аренда пользователя.
test("#423 two sessions of one owner: the puppet list is the user's lease, marked so", () => {
  const two: Master[] = [
    { project: "mop", master: "ermak.mate-7", addresses: ["ermak.mate-7"], identified: true,
      user: "ermak", session: "mop-ab", cwd: "-", puppets: ["pu-mop-1", "pu-mop-3"] },
    { project: "mop", master: "ermak.box-9", addresses: ["ermak.box-9"], identified: true,
      user: "ermak", session: "mop-cd", cwd: "-", puppets: ["pu-mop-1", "pu-mop-3"] },
  ];
  renderUi(<Masters masters={two} />);
  // Список в строке -- не молчаливая собственность сессии: подпись называет,
  // чей он (аренда логина в проекте).
  const row = screen.getByText("mop-ab").closest("tr")!;
  expect(within(row).getByText(/папеты пользователя/)).toBeInTheDocument();
  expect(within(row).getByText("pu-mop-1, pu-mop-3")).toBeInTheDocument();
});

// Переход: старый бэкенд без addresses и identified. Счётчик подтверждённых
// сессий -- 0: старый снимок не выдаётся за сессии, адреса считаются адресами.
test("#423 a legacy snapshot without identified counts 0 sessions", () => {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- снятие полей и есть тест
  const legacy = M.map(({ addresses, identified, ...rest }) => rest) as Master[];
  renderUi(<Masters masters={legacy} />);
  expect(screen.getByText("ermak.mate-7")).toBeInTheDocument();
  expect(screen.getByText("ivan.box-3")).toBeInTheDocument();
  expect(screen.getByText(/0 сессий, 2 по адресам/)).toBeInTheDocument();
});

// #326: период опроса -- из снимка (masters_every, #325), не своей копией.
test("#326 the poll period comes from the snapshot", () => {
  renderUi(<Masters masters={M} every={45} />);
  expect(screen.getByText(/опрос who раз в 45 с/)).toBeInTheDocument();
});

test("#326 a snapshot without the period: no invented number", () => {
  renderUi(<Masters masters={[]} />);
  expect(screen.getByText("опрос who")).toBeInTheDocument();
});

test("no masters: says so", () => {
  renderUi(<Masters masters={[]} />);
  expect(screen.getByText(EMPTY)).toBeInTheDocument();
});
