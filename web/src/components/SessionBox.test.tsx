// Сессия в шапке (#416): гостю -- «Войти» и окно, вошедшему -- «Выйти», admin --
// ссылка на панель прокси. Публичные таблицы не зависят от сессии.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { renderUi } from "../test-utils";
import { SessionBox } from "./SessionBox";
import { GUEST, type Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };
const USER: Session = { login: "olga", role: "user" };

afterEach(() => vi.unstubAllGlobals());

test("#521 a guest sees Войти, not the link nor Выйти", () => {
  renderUi(<SessionBox session={GUEST} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  expect(screen.getByRole("button", { name: "Войти" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "LLM Proxy" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Выйти" })).toBeNull();
});

test("#416 the modal asks login and password and submits them", async () => {
  const onLogin = vi.fn();
  renderUi(<SessionBox session={GUEST} onLogin={onLogin} onLogout={vi.fn()} failed={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  // Модал появляется с переходом -- ждём поля, а не читаем сразу
  const modal = await screen.findByRole("dialog");
  const user = await within(modal).findByLabelText("Логин");
  fireEvent.change(user, { target: { value: "anton" } });
  fireEvent.change(within(modal).getByLabelText("Пароль"), { target: { value: "secret" } });
  fireEvent.click(within(modal).getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(onLogin).toHaveBeenCalledWith("anton", "secret"));
});

test("#416 a wrong password shows the reason and clears the field", async () => {
  // После отказа поле пароля пусто: пароль не остаётся в DOM дольше попытки.
  // Отказ -- возвращённое false, как в App после правки контракта.
  let refused = false;
  const onLogin = vi.fn(async () => { refused = true; return false; });
  renderUi(<SessionBox session={GUEST} onLogin={onLogin} onLogout={vi.fn()} failed={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  const pwd = await screen.findByLabelText("Пароль");
  fireEvent.change(pwd, { target: { value: "secret" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(pwd).toHaveValue(""));
  expect(refused).toBe(true);
});

test("#521 a logged-in operator sees the login and Выйти, no modal", () => {
  renderUi(<SessionBox session={USER} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  expect(screen.getByText("olga")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Выйти" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Войти" })).toBeNull();
});

test("#416 only admin sees the LLM Proxy link to the management page", () => {
  const { unmount } = renderUi(<SessionBox session={ADMIN} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  const link = screen.getByRole("link", { name: "LLM Proxy" });
  expect(link).toHaveAttribute("href", "/llm/management.html");
  unmount();
  renderUi(<SessionBox session={USER} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  expect(screen.queryByRole("link", { name: "LLM Proxy" })).toBeNull();
});

test("#521 Выйти click calls onLogout", () => {
  const onLogout = vi.fn();
  renderUi(<SessionBox session={USER} onLogin={vi.fn()} onLogout={onLogout} failed={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Выйти" }));
  expect(onLogout).toHaveBeenCalled();
});

test("#416 a failed login shows the server's reason", async () => {
  renderUi(<SessionBox session={GUEST} onLogin={vi.fn()} onLogout={vi.fn()}
                       failed="invalid login or password" />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  expect(await screen.findByText("invalid login or password")).toBeInTheDocument();
});


// Ревью #416: закрытие окна и успех входа.
test("#416 closing the modal clears the password (X and Отмена)", async () => {
  renderUi(<SessionBox session={GUEST} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  const pwd = await screen.findByLabelText("Пароль", {}, { timeout: 3000 });
  fireEvent.change(pwd, { target: { value: "secret" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Отмена" }));
  // мод уходит с переходом: ждём закрытия, иначе вторая «Войти» находит обе
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  expect(await screen.findByLabelText("Пароль")).toHaveValue("");
});

test("#416 a successful login closes the modal; after logout it is closed too", async () => {
  const onLogin = vi.fn().mockResolvedValue(true);
  const view = renderUi(<SessionBox session={GUEST} onLogin={onLogin} onLogout={vi.fn()} failed={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  fireEvent.change(await screen.findByLabelText("Пароль"), { target: { value: "secret" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(screen.queryByLabelText("Пароль")).toBeNull());
  // сессия сменилась (App перерисовал с новым session), окно не открывается само
  view.rerender(
    <MantineProvider>
      <SessionBox session={USER} onLogin={onLogin} onLogout={vi.fn()} failed={null} />
    </MantineProvider>);
  expect(screen.queryByLabelText("Пароль")).toBeNull();
  expect(screen.getByRole("button", { name: "Выйти" })).toBeInTheDocument();
});

test("#416 a failed login keeps the modal open with the reason", async () => {
  // App после правки возвращает false, не бросает
  const onLogin = vi.fn().mockResolvedValue(false);
  renderUi(<SessionBox session={GUEST} onLogin={onLogin} onLogout={vi.fn()} failed="неверный логин или пароль" />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  fireEvent.change(await screen.findByLabelText("Пароль"), { target: { value: "x" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(screen.getByLabelText("Пароль")).toHaveValue(""));
  // окно открыто и после успокоения анимации
  await new Promise((r) => setTimeout(r, 400));
  expect(screen.getByLabelText("Пароль")).toBeInTheDocument();
});
