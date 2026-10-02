// Сессия в шапке (#416): гостю -- Login и окно, вошедшему -- Logout, admin --
// ссылка на панель прокси. Публичные таблицы не зависят от сессии.
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { renderUi } from "../test-utils";
import { SessionBox } from "./SessionBox";
import { GUEST, type Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };
const USER: Session = { login: "olga", role: "user" };

afterEach(() => vi.unstubAllGlobals());

test("#416 a guest sees Login, not the link nor Logout", () => {
  renderUi(<SessionBox session={GUEST} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  expect(screen.getByRole("button", { name: "Login" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "LLM Proxy" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Logout" })).toBeNull();
});

test("#416 the modal asks login and password and submits them", async () => {
  const onLogin = vi.fn();
  renderUi(<SessionBox session={GUEST} onLogin={onLogin} onLogout={vi.fn()} failed={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Login" }));
  // Модал появляется с переходом -- ждём поля, а не читаем сразу
  const user = await screen.findByLabelText("Логин");
  fireEvent.change(user, { target: { value: "anton" } });
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(onLogin).toHaveBeenCalledWith("anton", "secret"));
});

test("#416 a wrong password shows the reason and clears the field", async () => {
  // После отказа поле пароля пусто: пароль не остаётся в DOM дольше попытки.
  // Отказ обязан дойти до App (тот покажет причину): перехватываем тут,
  // чтобы проверка ждала именно отказа, а не висящего промиса.
  let refused = false;
  const onLogin = vi.fn(() =>
    Promise.reject(new Error("no")).finally(() => { refused = true; }));
  renderUi(<SessionBox session={GUEST} onLogin={onLogin} onLogout={vi.fn()} failed={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Login" }));
  const pwd = await screen.findByLabelText("Пароль");
  fireEvent.change(pwd, { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(pwd).toHaveValue(""));
  expect(refused).toBe(true);
});

test("#416 a logged-in operator sees the login and Logout, no modal", () => {
  renderUi(<SessionBox session={USER} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  expect(screen.getByText("olga")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Logout" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Login" })).toBeNull();
});

test("#416 only admin sees the LLM Proxy link to the management page", () => {
  const { unmount } = renderUi(<SessionBox session={ADMIN} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  const link = screen.getByRole("link", { name: "LLM Proxy" });
  expect(link).toHaveAttribute("href", "/llm/management.html");
  unmount();
  renderUi(<SessionBox session={USER} onLogin={vi.fn()} onLogout={vi.fn()} failed={null} />);
  expect(screen.queryByRole("link", { name: "LLM Proxy" })).toBeNull();
});

test("#416 Logout click calls onLogout", () => {
  const onLogout = vi.fn();
  renderUi(<SessionBox session={USER} onLogin={vi.fn()} onLogout={onLogout} failed={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Logout" }));
  expect(onLogout).toHaveBeenCalled();
});

test("#416 a failed login shows the server's reason", async () => {
  renderUi(<SessionBox session={GUEST} onLogin={vi.fn()} onLogout={vi.fn()}
                       failed="invalid login or password" />);
  fireEvent.click(screen.getByRole("button", { name: "Login" }));
  expect(await screen.findByText("invalid login or password")).toBeInTheDocument();
});
