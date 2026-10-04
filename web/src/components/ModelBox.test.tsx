// Форма смены модели на карточке папета (#468): выбор из каталога прокси
// (GET /api/admin/models, один источник -- useModels), подтверждение --
// точная цель, POST {action: "model", target, params: {model}} -- тот же
// общий ход операции (useAdminOp). Вердикт -- слова opWord (#465), «dialog
// closed» подтверждением не считается: succeeded приезжает только после
// телеметрии (sessionId + свежая модель), это сервер гарантирует.
// Радио, а не Select: Mantine Select не открывается в jsdom (#472 опыт).
// Результат в памяти страницы, перезагрузка начинает заново.
//
// RESULT: тесты написаны до реализации (RED).
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ModelBox } from "./ModelBox";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

const ROW: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};

// #488: у папета есть текущая модель -- диалог обязан её показать.
const LIVE_ROW: Puppet = { ...ROW, llm: "glm-5.3-flash" };

const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "model",
     target: "pu-mop-1", status }) }) as Response;
const catalog = () =>
  ({ ok: true, status: 200, json: async () => ({
    models: ["claude-opus-5-5", "glm-5.3", "glm-5.3-flash"] }) }) as Response;
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) =>
    String(url).includes("/api/admin/models") ? catalog()
      : (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const openBox = async () => {
  fireEvent.click(screen.getByRole("button", { name: /модель/i }));
  await screen.findByTestId("model-choices");
};

/** Пройти до POST: открыть, выбрать модель, подтвердить. */
const ride = async (model: RegExp) => {
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: model }));
  fireEvent.click(screen.getByRole("button", { name: /^продолжить$/i }));
  fireEvent.click(await screen.findByRole("button", { name: /^подтвердить$/i }));
};

test("the card offers a model switch with the catalog as radios, no preselection", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => catalog()));
  renderUi(<ModelBox session={ADMIN} row={ROW} />);
  await openBox();
  for (const m of ["claude-opus-5-5", "glm-5.3-flash"]) {
    expect(screen.getByRole("radio", { name: new RegExp(m, "i") })).toBeInTheDocument();
  }
  // ни один вариант не выбран заранее: смена без выбора невозможна
  const checked = screen.getAllByRole("radio").filter((r) => (r as HTMLInputElement).checked);
  expect(checked).toHaveLength(0);
});

test("#488 the current model is marked and not selectable", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => catalog()));
  renderUi(<ModelBox session={ADMIN} row={LIVE_ROW} />);
  await openBox();
  const cur = screen.getByRole("radio", { name: /текущая.*glm-5\.3-flash|glm-5\.3-flash.*текущая/i });
  expect(cur).toBeDisabled();
  // остальные -- живые для выбора
  expect(screen.getByRole("radio", { name: /^glm-5\.3$/i })).toBeEnabled();
});

// #488: шпион балунов на уровне модуля -- vi.mock поднимается наверх.
const shown: unknown[] = [];
vi.mock("@mantine/notifications", async (importOriginal) => {
  const orig = await importOriginal<typeof import("@mantine/notifications")>();
  return { ...orig, notifications: { ...orig.notifications,
    show: (n: unknown) => shown.push(n) } };
});

test("#488 verdicts ride toast balloons, not an inline line", async () => {
  shown.length = 0;
  vi.stubGlobal("fetch", postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("refused"))));
  renderUi(<ModelBox session={ADMIN} row={LIVE_ROW} />);
  await ride(/claude-opus-5-5/i);
  await waitFor(() => expect(shown.length).toBeGreaterThan(0));
  const last = shown[shown.length - 1] as { message?: string; color?: string };
  expect(String(last.message)).toContain("отказано");
});

test("#488 after a terminal verdict the pool snapshot is refetched", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<ModelBox session={ADMIN} row={LIVE_ROW} />);
  await ride(/claude-opus-5-5/i);
  await waitFor(() => expect(fetchMock.mock.calls
    .filter((c) => String(c[0]).endsWith("/api/pool"))).toHaveLength(1));
});

test("the catalog comes from the server facade, not a homemade list", async () => {
  const fetchMock = vi.fn(async (url: string | URL) =>
    String(url).includes("/api/admin/models") ? catalog() : catalog());
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<ModelBox session={ADMIN} row={ROW} />);
  await openBox();
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/models",
    expect.objectContaining({ credentials: "same-origin" }));
  // чужой модели в списке нет: страница не дорисовывает свой список
  expect(screen.queryByRole("radio", { name: /fantasy-model/ })).toBeNull();
});

test("confirm shows the exact target before the POST", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<ModelBox session={ADMIN} row={ROW} />);
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: /glm-5\.3-flash/i }));
  fireEvent.click(screen.getByRole("button", { name: /^продолжить$/i }));
  const dialog = (await screen.findAllByRole("dialog"))
    .find((d) => d.textContent.includes("подтвердите смену модели"));
  expect(dialog?.textContent).toContain("pu-mop-1");
  expect(dialog?.textContent).toContain("hyper");
  expect(dialog?.textContent).toContain("glm-5.3-flash");
  // Подтверждение без сети: до клика «подтвердить» POST не уходит (GET
  // каталога при открытии формы -- законное чтение, не исполнение).
  expect(fetchMock.mock.calls
         .filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(0);
});

test("the choice posts {action: model, target, params: {model}}", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<ModelBox session={ADMIN} row={ROW} />);
  await ride(/glm-5\.3-flash/i);
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  expect(JSON.parse((post?.[1] as { body: string }).body))
    .toEqual({ action: "model", target: "pu-mop-1",
               params: { model: "glm-5.3-flash" } });
});

test("without a choice the continue button is locked and nothing posts", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<ModelBox session={ADMIN} row={ROW} />);
  await openBox();
  expect(screen.getByRole("button", { name: /^продолжить$/i })).toBeDisabled();
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(0);
});

test("succeeded reads the confirmed word, unknown is named unknown", async () => {
  shown.length = 0;
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<ModelBox session={ADMIN} row={ROW} />);
  await ride(/glm-5\.3-flash/i);
  await waitFor(() => expect(shown.length).toBeGreaterThan(0));
  expect(String((shown[shown.length - 1] as { message?: string }).message))
    .toMatch(/исход неизвестен/i);
});

test("a refusal names the server phrase (the dialog did not close, a foreign lease)", async () => {
  shown.length = 0;
  const refused = () =>
    ({ ok: true, status: 200, json: async () => ({ id: "op-1",
       action: "model", target: "pu-mop-1", status: "refused",
       error: "pu-mop-1: model switch dialog did not close" }) }) as Response;
  const fetchMock = vi.fn(async (url: string | URL, init?: { method?: string }) =>
    String(url).includes("/api/admin/models") ? catalog()
      : (init?.method ?? "GET") === "POST" ? ok202("op-1") : refused());
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<ModelBox session={ADMIN} row={ROW} />);
  await ride(/glm-5\.3-flash/i);
  await waitFor(() => expect(shown.length).toBeGreaterThan(0));
  expect(String((shown[shown.length - 1] as { message?: string }).message))
    .toMatch(/отказано/i);
  expect(String((shown[shown.length - 1] as { message?: string }).message))
    .toMatch(/dialog did not close/);
});

test("a guest and a user see no model button", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<ModelBox session={{ login: null, role: null }} row={ROW} />);
  expect(screen.queryByRole("button", { name: /модель/i })).toBeNull();
});
