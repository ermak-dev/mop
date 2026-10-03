// Карточка папета (#463): вход в карточку -- из строки таблицы пула, только
// для admin; сессия и цель живут в App, смена роли/цели и logout очищают.
//
// RESULT: тесты написаны до реализации (RED).
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";
import { emptySnapshot, renderUi } from "./test-utils";
import type { Snapshot } from "./types";

const SNAP: Snapshot = emptySnapshot({
  at: 1,
  projects: [{ name: "mop", counts: {
    puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0,
  }, puppets: [{
    name: "pu-mop-1", node: "hyper", alloc_status: "running", state: "free (master)",
    kind: "free", bucket: "free", detail: null, branch: "master",
    owner: "", llm: "claude", origin: "git@h:g/mop.git", disk_kb: null,
  }] }],
});

vi.mock("./usePool", () => ({ usePool: () => ({ snapshot: SNAP }) }));

vi.mock("./useSession", async () => {
  const real = await vi.importActual<typeof import("./useSession")>("./useSession");
  const state = { current: real.GUEST };
  return {
    ...real,
    fetchSession: () => Promise.resolve(state.current),
    postLogin: (login: string) => {
      if (login === "anton") { state.current = { login, role: "admin" }; return Promise.resolve(state.current); }
      if (login === "bob") { state.current = { login, role: "user" }; return Promise.resolve(state.current); }
      return Promise.reject(new real.LoginRefused(401, "invalid login or password"));
    },
    postLogout: () => { state.current = real.GUEST; return Promise.resolve(real.GUEST); },
    __reset: (s: typeof real.GUEST) => { state.current = s; },
    __state: () => state.current,
  };
});

const cardOk = () => ({
  ok: true, status: 200,
  json: async () => ({ puppet: SNAP.projects[0].puppets[0] }),
}) as Response;

const mocked = vi.mocked(await import("./useSession")) as unknown as {
  __reset?: (s: import("./useSession").Session) => void;
};

beforeEach(() => mocked.__reset?.({ login: "anton", role: "admin" }));
afterEach(() => vi.unstubAllGlobals());

test("admin opens the card from the row; a guest has no opener", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => cardOk()));
  const view = renderUi(<App />);
  await waitFor(() => expect(screen.getByText("pu-mop-1")).toBeInTheDocument());
  // admin: строка открывается кнопкой с доступным именем
  const opener = screen.getByRole("button", { name: /pu-mop-1/i });
  fireEvent.click(opener);
  await waitFor(() => expect(screen.getByText("git@h:g/mop.git")).toBeInTheDocument());
  view.unmount();
});

test("a user without the admin role has no opener at all", async () => {
  mocked.__reset?.({ login: "bob", role: "user" });
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<App />);
  await waitFor(() => expect(screen.getByText("pu-mop-1")).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: /pu-mop-1/i })).toBeNull();
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

test("logout with the card open clears it in the same render", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => cardOk()));
  renderUi(<App />);
  // сессия admin приезжает асинхронно -- ждём открывашку, не кликаем вслепую
  fireEvent.click(await screen.findByRole("button", { name: /pu-mop-1/i }));
  // карточка загрузилась: сырая state-строка -- факт только карточки
  await waitFor(() => expect(screen.getByText("free (master)")).toBeInTheDocument());
  // выход из шапки: карточка закрывается, факты уходят с экрана. Признак --
  // сырая state-строка: она только в карточке, таблица показывает слово.
  fireEvent.click(screen.getByRole("button", { name: /logout/i }));
  await waitFor(() => expect(screen.queryByText("free (master)")).toBeNull());
  // Mantine держит пустой корень модала в DOM -- пустота и есть признак
  const dialog = screen.queryByRole("dialog");
  expect(dialog?.textContent ?? "").toBe("");
  // гость снова видит имя строкой, без открывашки
  expect(screen.queryByRole("button", { name: /pu-mop-1/i })).toBeNull();
});

test("switching the target while pending never swaps in the old card", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  vi.stubGlobal("fetch", vi.fn(async () => slow));
  renderUi(<App />);
  fireEvent.click(await screen.findByRole("button", { name: /pu-mop-1/i }));
  // карточка ещё грузится; пользователь закрывает её
  fireEvent.click(await screen.findByRole("button", { name: /закрыть/i }));
  // корень модала Mantine держит в DOM -- содержимое уходит. Сырая
  // state-строка -- факт карточки, в таблице её текста нет.
  await waitFor(() => expect(screen.queryByText("free (master)")).toBeNull());
  // Mantine держит пустой корень модала в DOM -- пустота и есть признак
  expect(screen.queryByRole("dialog")?.textContent ?? "").toBe("");
  // опоздавший ответ первой открытой карточки приходит после закрытия
  await act(async () => { release?.(cardOk()); });
  await new Promise((r) => setTimeout(r, 20));
  // закрытая цель не воскресает: карточки нет вовсе (origin в заголовке
  // секции -- публичный факт, признак -- отсутствие диалога и state-строки)
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.queryByText("free (master)")).toBeNull();
});
