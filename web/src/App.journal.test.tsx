// Интеграция #422 в приложение: журнал -- отдельная страница за хешем,
// ссылка на неё видна только админу, главная больше не несёт встроенный
// журнал (он ушёл из публичного снимка, #424). Доступ к #journal без
// прав показывает отказ, а не пустую страницу.
//
// HYPOTHESIS (#422): App держит страницу по useHashPage, журнал грузит
// useJournal при admin, обе вкладки живут на одном снимке usePool.
// RESULT: тесты написаны до реализации (RED).
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App";
import { emptySnapshot, renderUi } from "./test-utils";
import type { JournalEntry } from "./types";
import type { Session } from "./useSession";

const SNAP = emptySnapshot({ at: 1 });
const J: JournalEntry[] = [
  { at: 1700000000, event: "send", name: "pu-mop-1", node: "hyper", project: "mop", text: "from ermak" },
];

vi.mock("./usePool", () => ({ usePool: () => ({ snapshot: SNAP }) }));

// Сессия и журнал -- управляемые моки: состояние модуля, как в App.test.
vi.mock("./useSession", async () => {
  const real = await vi.importActual<typeof import("./useSession")>("./useSession");
  const state = { current: real.GUEST as Session };
  return {
    ...real,
    fetchSession: () => Promise.resolve(state.current),
    postLogin: (login: string) => {
      state.current = { login, role: "admin" };
      return Promise.resolve(state.current);
    },
    postLogout: () => { state.current = real.GUEST; return Promise.resolve(real.GUEST); },
    __set: (s: Session) => { state.current = s; },
  };
});
vi.mock("./useJournal", () => {
  const state = { journal: [] as JournalEntry[], error: null as string | null };
  return {
    useJournal: (_session: unknown, active: boolean) => (active ? state : { journal: [], error: null }),
    __setJournal: (j: JournalEntry[], e: string | null) => { state.journal = j; state.error = e; },
  };
});

const mocks = async () => ({
  session: vi.mocked(await import("./useSession")) as unknown as { __set: (s: Session) => void },
  journal: vi.mocked(await import("./useJournal")) as unknown as {
    __setJournal: (j: JournalEntry[], e: string | null) => void;
  },
});

beforeEach(async () => {
  window.location.hash = "";
  const m = await mocks();
  m.session.__set({ login: null, role: null });
  m.journal.__setJournal([], null);
});

test("#422 the main page has no embedded journal any more", () => {
  renderUi(<App />);
  expect(screen.queryByTestId("journal")).toBeNull();
});

test("#422 a guest and a plain user see no link to the journal page", async () => {
  const m = await mocks();
  renderUi(<App />);
  expect(screen.queryByRole("button", { name: "Журнал" })).toBeNull();
  m.session.__set({ login: "olga", role: "user" });
  fireEvent.focus(window);
  await waitFor(() => expect(screen.getByText("olga")).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Журнал" })).toBeNull();
});

test("#422 admin follows the link to #journal and sees the events", async () => {
  const m = await mocks();
  m.session.__set({ login: "anton", role: "admin" });
  m.journal.__setJournal(J, null);
  renderUi(<App />);
  // Anchor component="button" -- в DOM кнопка с aria-label
  const link = await screen.findByRole("button", { name: "Журнал" });
  fireEvent.click(link);
  await waitFor(() => expect(window.location.hash).toBe("#journal"));
  expect(await screen.findByText("pu-mop-1")).toBeInTheDocument();
});

test("#422 opening #journal as a guest shows the refusal, not events", async () => {
  const m = await mocks();
  // реальный хук ставит отказ сам; здесь его роль играет мок (поведение
  // хука -- в useJournal.test), поэтому отказ кладём как он бы это сделал
  m.journal.__setJournal([], "журнал недоступен: прав не хватает");
  window.location.hash = "#journal";
  renderUi(<App />);
  await waitFor(() => expect(screen.getByText(/журнал недоступен/)).toBeInTheDocument());
  expect(screen.queryByText("pu-mop-1")).toBeNull();
});

// Ревью 2 (#422): открытый popup не переживает logout/разжалование -- и
// не эффектом после paint, а тем же рендером: denial проходит в App
// синхронно, JournalPage скрывает таблицу и модал до первой перерисовки
// браузера. Здесь -- жизненный путь: admin открыл событие, cookie истекла,
// опрос ответил 401 и хук положил отказ (мок кладёт его как хук бы).
test("#422 review2: logout with an open popup clears it without a stale frame", async () => {
  const m = await mocks();
  m.session.__set({ login: "anton", role: "admin" });
  m.journal.__setJournal(J, null);
  renderUi(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Журнал" }));
  await waitFor(() => expect(window.location.hash).toBe("#journal"));
  // открыли полное событие
  fireEvent.click(screen.getByText(/сообщение от ermak/));
  await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
  // cookie истекла: опрос ответил 401 -- хук вернул отказ; сессия уже гость
  m.journal.__setJournal([], "журнал недоступен: прав не хватает");
  m.session.__set({ login: null, role: null });
  fireEvent.focus(window);   // App перерисовался -- состояние мока прочитано
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByText(/прав не хватает/)).toBeInTheDocument();
  expect(screen.queryByText(/сообщение от ermak/)).toBeNull();
});

test("#422 back from the journal returns to the main page", async () => {
  const m = await mocks();
  m.session.__set({ login: "anton", role: "admin" });
  m.journal.__setJournal(J, null);
  renderUi(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Журнал" }));
  await waitFor(() => expect(window.location.hash).toBe("#journal"));
  fireEvent.click(screen.getByRole("button", { name: "главная" }));
  await waitFor(() => expect(window.location.hash).toBe(""));
  // главная: таблицы пула снова на экране (снимок пуст -- заметка вместо
  // таблиц, но страница журнала ушла)
  expect(screen.queryByTestId("journal-page")).toBeNull();
  expect(screen.getByText("папетов нет")).toBeInTheDocument();
});
