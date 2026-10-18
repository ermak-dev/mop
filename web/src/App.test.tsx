// Скелет (#297): шапка и счётчики корзин из снимка.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import App from "./App";
import { emptySnapshot, renderUi } from "./test-utils";

const SNAP = emptySnapshot({
  at: 1, errors: ["nomad: no connection"],
  counts: { puppets: 3, free: 1, busy: 2, sick: 0, silent: 0, down: 0 },
});

// Снимок -- состояние теста (#504): проверка вердиктов подменяет его,
// остальной файл живёт с дефолтным SNAP (тот же приём, что state у мока
// useSession ниже). Фабрика vi.mock поднимается выше const SNAP, поэтому
// держатель -- vi.hoisted, а сам SNAP читается только в момент вызова.
const poolState = vi.hoisted(() => ({ snap: null as import("./types").Snapshot | null }));
vi.mock("./usePool", () => ({
  usePool: () => ({ snapshot: poolState.snap ?? SNAP }),
}));

// #416: сессия -- гость по умолчанию, тест меняет состоянием модуля.
vi.mock("./useSession", async () => {
  const real = await vi.importActual<typeof import("./useSession")>("./useSession");
  const state = { current: real.GUEST };
  return {
    ...real,
    fetchSession: () => Promise.resolve(state.current),
    postLogin: (login: string) => {
      if (login === "anton") { state.current = { login, role: "admin" }; return Promise.resolve(state.current); }
      return Promise.reject(new real.LoginRefused(401, "invalid login or password"));
    },
    postLogout: () => { state.current = real.GUEST; return Promise.resolve(real.GUEST); },
    __reset: (s: typeof real.GUEST) => { state.current = s; },
    __state: () => state.current,
  };
});
// __reset есть только в моке; берём его из живого реестра модулей
const mocked = vi.mocked(await import("./useSession")) as unknown as {
  __reset?: (s: import("./useSession").Session) => void;
};

test("the header shows the counters and the collector's errors", () => {
  renderUi(<App />);
  expect(screen.queryByText(/Master Of Puppets?/i)).toBeNull();
  expect(document.querySelector('img[src="/logo.png"]')).toBeInTheDocument();
  expect(screen.getByText("3 папета")).toBeInTheDocument();
  expect(screen.getByText("1 свободны")).toBeInTheDocument();
  expect(screen.getByText("2 заняты")).toBeInTheDocument();
  expect(screen.queryByText(/больны/)).toBeNull();
  expect(screen.getByText("nomad: no connection")).toBeInTheDocument();
});

// #378: одна строка бейджей (места и корзины) и полоса аллокаций под шапкой.
test("#378 one row of badges and the allocation bar", () => {
  renderUi(<App />);
  expect(screen.getAllByTestId("chips")).toHaveLength(1);
  expect(screen.getByText("всего 0 мест")).toBeInTheDocument();
  expect(screen.getByTestId("load-bar")).toBeInTheDocument();
});

// HYPOTHESIS (#446): вкладки страниц идут первым рядом, а полоса вместе с
// логотипом, бейджами и секциями лежит внутри одного рабочего контейнера.
// SOLUTION: Mantine Tabs в навигации; с #664 общий fluid-контейнер dashboard.
// RESULT: порядок и общий контейнер проверены. STATUS: FIXED — see #446.
test("#446 navigation precedes branding and the load bar stays inside the workspace", () => {
  renderUi(<App />);
  const nav = screen.getByRole("tablist", { name: "страницы" });
  const main = screen.getByRole("tab", { name: "Пул" });
  const logo = document.querySelector('img[src="/logo.png"]');
  const badges = screen.getByTestId("chips");
  const bar = screen.getByTestId("load-bar");
  expect(main).toHaveAttribute("aria-selected", "true");
  expect(nav).toContainElement(main);
  expect(logo).toBeInTheDocument();
  expect(nav.compareDocumentPosition(logo!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(logo!.compareDocumentPosition(badges)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(badges.compareDocumentPosition(bar)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(bar.closest(".dashboard")).toBe(nav.closest(".dashboard"));
  expect(bar.closest(".dashboard")).toBe(screen.getByText("nomad: no connection").closest(".dashboard"));
});

// Подвала нет (просьба оператора 27.09, #301): строка «поток событий · без
// входа… · JSON» убрана со страницы.
test("no footer line", () => {
  renderUi(<App />);
  expect(screen.queryByText(/без входа, LAN доверенная/)).toBeNull();
  expect(screen.queryByText("JSON")).toBeNull();
});

// #378: окно раскатки -- web/dist новый, а сервер ещё отдаёт снимок без
// load. Шапка -- прежняя (папеты и корзины), полосы нет, страница не белая.
test("#378 a snapshot without load keeps the old header and draws no bar", () => {
  // Последняя проверка файла: мок usePool отдаёт этот же объект.
  delete SNAP.load;
  renderUi(<App />);
  expect(screen.getByText("3 папета")).toBeInTheDocument();
  expect(screen.getByText("2 заняты")).toBeInTheDocument();
  expect(screen.queryByText(/мест/)).toBeNull();
  expect(screen.queryByTestId("load-bar")).toBeNull();
});

// #416: сессия в шапке. Гость -- «Войти», вошедший -- «Выйти» и логин, admin --
// ссылка на панель прокси. Таблицы публичные -- остаются при любом состоянии.
const reset = (s: Session) => {
  mocked.__reset?.(s);
  // тесты выше подменяли fetchSession своей; reset возвращает читающую state
  (mocked as { fetchSession?: unknown }).fetchSession = () =>
    Promise.resolve((mocked as { __state?: () => Session }).__state?.()
      ?? { login: null, role: null });
};
type Session = import("./useSession").Session;

test("#521 a guest header has Войти, an operator's has Выйти", async () => {
  renderUi(<App />);
  expect(screen.getByRole("button", { name: "Войти" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Выйти" })).toBeNull();
});

test("#416 login flow: the session box appears after a successful POST", async () => {
  renderUi(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  const modal = await screen.findByRole("dialog");
  fireEvent.change(within(modal).getByLabelText("Логин"), { target: { value: "anton" } });
  fireEvent.change(within(modal).getByLabelText("Пароль"), { target: { value: "secret" } });
  fireEvent.click(within(modal).getByRole("button", { name: "Войти" }));
  expect(await screen.findByText("anton")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Выйти" })).toBeInTheDocument();
});

test("#416 a wrong password shows the localized reason, password field empty", async () => {
  reset({ login: null, role: null }); // прошлый тест вошёл антоном
  renderUi(<App />);
  // ...тело ниже осталось, см. новый интеграционный тест ревью
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  // модал появляется с переходом; ждём оба поля, потом меняем
  const user = await screen.findByLabelText("Логин", {}, { timeout: 3000 });
  const pwd = await screen.findByLabelText("Пароль", {}, { timeout: 3000 });
  fireEvent.change(user, { target: { value: "olga" } });
  fireEvent.change(pwd, { target: { value: "wrong" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  // причина приходит асинхронно (postLogin -> setState)
  await waitFor(() => expect(screen.getByText("неверный логин или пароль")).toBeInTheDocument());
  expect(screen.getByLabelText("Пароль")).toHaveValue("");
});

test("#416 logout returns to the guest header", async () => {
  reset({ login: "anton", role: "admin" });
  renderUi(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Выйти" }));
  expect(await screen.findByRole("button", { name: "Войти" })).toBeInTheDocument();
});

test("#416 admin sees the LLM Proxy link, user does not", async () => {
  // fetchSession по умолчанию -- гость; логинимся антоном (admin)
  const box = renderUi(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  fireEvent.change(await screen.findByLabelText("Логин", {}, { timeout: 2000 }),
                   { target: { value: "anton" } });
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "x" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  const link = await screen.findByRole("link", { name: "LLM Proxy" }, { timeout: 2000 });
  expect(link).toHaveAttribute("href", "/llm/management.html");
  box.unmount();
  reset({ login: "olga", role: "user" });
  renderUi(<App />);
  await screen.findByText("olga");
  expect(screen.queryByRole("link", { name: "LLM Proxy" })).toBeNull();
});


// Ревью #416: гонка стартового опроса, ошибка панели, обновление по focus.
test("#416 a late initial fetch cannot overwrite a fresher login", async () => {
  // первый fetchSession подвисает; вход успевает раньше -- ответ гостя не приходит
  const hanging = { calls: 0 };
  const slow: () => Promise<Session> = () => {
    hanging.calls++;
    // первый опрос не отвечает никогда: вход успевает раньше
    return hanging.calls === 1 ? new Promise<Session>(() => {})
                               : Promise.resolve({ login: "anton", role: "admin" });
  };
  (vi.mocked(await import("./useSession")) as { fetchSession: unknown }).fetchSession = slow;
  renderUi(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  fireEvent.change(await screen.findByLabelText("Логин", {}, { timeout: 3000 }),
                   { target: { value: "anton" } });
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "x" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(screen.getByText("anton")).toBeInTheDocument());
  await new Promise((r) => setTimeout(r, 50)); // подвисший опрос не ответил -- и не должен
  expect(screen.getByText("anton")).toBeInTheDocument();
});

test("#416 a successful login clears a stale panel error", async () => {
  // стартовый fetch падает 5xx, потом вход успешен: ошибки панели больше нет
  const mod = vi.mocked(await import("./useSession")) as unknown as {
    fetchSession: () => Promise<Session>;
  };
  const original = mod.fetchSession;
  mod.fetchSession = () => Promise.reject(new Error("5xx"));
  renderUi(<App />);
  await waitFor(() => expect(screen.getByText("панель недоступна")).toBeInTheDocument());
  mod.fetchSession = original;
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  fireEvent.change(await screen.findByLabelText("Логин", {}, { timeout: 3000 }),
                   { target: { value: "anton" } });
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "x" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(screen.queryByText("панель недоступна")).toBeNull());
  expect(screen.getByText("anton")).toBeInTheDocument();
});

test("#416 focus re-checks the session: TTL turned an admin into a guest", async () => {
  reset({ login: "anton", role: "admin" });
  renderUi(<App />);
  await screen.findByText("anton");
  reset({ login: null, role: null }); // cookie истекла
  fireEvent.focus(window);
  await waitFor(() => expect(screen.queryByText("anton")).toBeNull());
  expect(screen.getByRole("button", { name: "Войти" })).toBeInTheDocument();
});


// Ревью 2 (#416): App.login глотает отказ и возвращает fulfilled Promise --
// с настоящим (не подменённым) onLogin окно обязано остаться открытым.
test("#416 integration: after a 401 the modal stays open with the reason", async () => {
  reset({ login: null, role: null });
  renderUi(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  fireEvent.change(await screen.findByLabelText("Логин", {}, { timeout: 3000 }),
                   { target: { value: "olga" } }); // мок отказывает всем, кроме anton
  fireEvent.change(await screen.findByLabelText("Пароль", {}, { timeout: 3000 }),
                   { target: { value: "wrong" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(screen.getByText("неверный логин или пароль")).toBeInTheDocument());
  // ждём дольше выхода анимации модала: закрытое окно убирает поля из DOM,
  // открытое -- держит их и причину рядом
  await new Promise((r) => setTimeout(r, 400));
  expect(screen.getByText("неверный логин или пароль")).toBeInTheDocument();
  expect(screen.getByLabelText("Пароль")).toHaveValue("");
  // модал открыт: его «Войти» на месте (шапочная закрыта за ним)
  expect(within(screen.getByRole("dialog"))
    .getByRole("button", { name: "Войти" })).toBeInTheDocument();
});

// Ревью 2 (#416): поздний стартовый опрос обязан РАЗРЕШИТЬСЯ гостем уже
// после успешного входа -- и не затереть его.
test("#416 a late guest answer arriving after login leaves the login alone", async () => {
  reset({ login: null, role: null });
  let release: (s: Session) => void = () => {};
  const gate = new Promise<Session>((r) => { release = r; });
  (vi.mocked(await import("./useSession")) as { fetchSession: unknown }).fetchSession =
    () => gate as Promise<Session>;
  renderUi(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  fireEvent.change(await screen.findByLabelText("Логин", {}, { timeout: 3000 }),
                   { target: { value: "anton" } });
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "x" } });
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Войти" }));
  await waitFor(() => expect(screen.getByText("anton")).toBeInTheDocument());
  release({ login: null, role: null }); // опоздавший ответ -- гость
  await new Promise((r) => setTimeout(r, 50));
  expect(screen.getByText("anton")).toBeInTheDocument();
});


// Ревью 3 (#416): focus и visibility стартуют два sync с одной эпохой --
// если старший ответ придёт позже, он не имеет права менять состояние.
test("#416 out-of-order session answers: the newest request wins", async () => {
  // первая фаза: вошли (admin) -- как в жизни, входом; упрощаем: стартовый
  // sync отвечает admin, страница показывает антона
  (vi.mocked(await import("./useSession")) as { fetchSession: unknown }).fetchSession =
    () => Promise.resolve({ login: "anton", role: "admin" });
  renderUi(<App />);
  await screen.findByText("anton");

  // вторая фаза: mount-подобный «старый» опрос висит, «новый» (focus) уже
  // знает, что cookie истекла -- гость
  let stale: (s: Session) => void = () => {};
  const hold = new Promise<Session>((r) => { stale = r; });
  let calls = 0;
  (vi.mocked(await import("./useSession")) as { fetchSession: unknown }).fetchSession = () => {
    calls++;
    return calls === 1 ? (hold as Promise<Session>) : Promise.resolve({ login: null, role: null });
  };
  // как в жизни: focus и visibility стартуют два sync почти сразу;
  // первый (старый) висит, второй (новый) отвечает гостем
  fireEvent.focus(window);
  fireEvent(document, new Event("visibilitychange"));
  await waitFor(() => expect(screen.queryByText("anton")).toBeNull());
  stale({ login: "anton", role: "admin" }); // опоздавший старый ответ: admin
  await new Promise((r) => setTimeout(r, 100));
  // старый ответ не возвращает админа: последний начатый sync решает
  expect(screen.queryByText("anton")).toBeNull();
  expect(screen.getByRole("button", { name: "Войти" })).toBeInTheDocument();
});

// #627/#643: create API уже принимает конечный intent; вместо устаревшей
// заглушки оператор открывает форму с явными id. Гостю вход не показывается.
const NOT_EXPOSED = "Creation is not yet exposed by the new controller.";
test("#627 operators get the create form entry instead of the obsolete placeholder", async () => {
  (vi.mocked(await import("./useSession")) as { fetchSession: unknown }).fetchSession =
    () => Promise.resolve({ login: "anton", role: "admin" });
  const box = renderUi(<App />);
  await screen.findByText("anton");
  expect(screen.getByRole("button", { name: /завести папета/i })).toBeInTheDocument();
  expect(screen.queryByText(NOT_EXPOSED)).toBeNull();
  box.unmount();

  (vi.mocked(await import("./useSession")) as { fetchSession: unknown }).fetchSession =
    () => Promise.resolve({ login: "olga", role: "user" });
  const box2 = renderUi(<App />);
  await screen.findByText("olga");
  expect(screen.queryByRole("button", { name: /завести папета/i })).toBeNull();
  expect(screen.queryByText(NOT_EXPOSED)).toBeNull();
  box2.unmount();
});

// ── #509: терминал кадра -- балун, не карточка строки ──────────────────
// HYPOTHESIS: вердикт чужой операции (id не из 202 этой страницы) виден
// был только в журнале, страница о нём молчала; карточка-вердикт на
// строке (#504) ушла -- слово дублировало бы балун и расширяло статус.
// SOLUTION: App подписан на шину кадров (useOperationFeed), терминальный
// кадр с чужим id показывает балун по правилам useOperationNotice: слово,
// цвет, имя цели в заголовке; unknown -- жёлтый со ссылкой в журнал.
// RESULT: тесты написаны до реализации (RED).
import { act } from "@testing-library/react";
import { operations } from "./useOperationFeed";

const WITH_PUPPET = emptySnapshot({
  at: 1,
  projects: [{ name: "mop",
    counts: { puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0 },
    puppets: [{ name: "pu-mop-1", node: "hyper", alloc_status: "running",
      state: "free (master)", kind: "free", bucket: "free", detail: null,
      branch: "master", owner: "", llm: "claude",
      origin: "git@h:g/mop.git", disk_kb: null }] }],
});

const shown: unknown[] = [];
vi.mock("@mantine/notifications", async (importOriginal) => {
  const orig = await importOriginal<typeof import("@mantine/notifications")>();
  return { ...orig, notifications: { ...orig.notifications,
    show: (n: unknown) => shown.push(n) } };
});

test("#509 a foreign unknown frame balloons, the row stays clean", async () => {
  shown.length = 0;
  poolState.snap = WITH_PUPPET;
  reset({ login: "anton", role: "admin" });
  renderUi(<App />);
  await screen.findByText("pu-mop-1");
  // Кадр unknown с чужим id (не из 202 этой страницы): балун со словом
  // канала и ссылкой в журнал, карточки на строке нет. Разбор SSE живёт в
  // usePool (проверен своим файлом); кадр входит шиной канала -- тот же
  // вход, что даёт слушатель потока.
  await act(async () => {
    operations.frame({ id: "op-7", action: "restart",
                       target: "pu-mop-1", status: "unknown" });
  });
  await waitFor(() => expect(shown).toHaveLength(1));
  const balloon = shown[0] as { title?: string; message?: unknown; color?: string };
  expect(String(balloon.title)).toContain("pu-mop-1");
  // сообщение unknown -- JSX со ссылкой: смотрим плоско
  expect(JSON.stringify(balloon.message)).toContain("Исход неизвестен");
  expect(JSON.stringify(balloon.message)).toContain("#journal");
  expect(balloon.color).toBe("yellow");
  expect(screen.queryByTestId("target-verdict-pu-mop-1")).toBeNull();
  poolState.snap = null;
});
