// Форма update на карточке папета (#472): fresh как явный выбор
// keep/replace/remove -- никаких None и умолчаний «как-нибудь». Последствия
// -- слова #473 (preview), при каждом выборе. POST /api/admin/ops
// {action: "update", target, params} -- параметры те же, что и выбор:
// fresh=false (keep), fresh=true (replace), fresh=true + origin (remove).
//
// RESULT: тесты написаны до реализации (RED).
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { UpdateBox } from "./UpdateBox";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

const ROW: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};

const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "update",
     target: "pu-mop-1", status }) }) as Response;
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) =>
    (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const openBox = async () => {
  fireEvent.click(screen.getByRole("button", { name: /правка/i }));
  await screen.findByTestId("update-modes");
};

/** Пройти до POST: открыть, выбрать режим, подтвердить. */
const ride = async (mode: string, phrase: RegExp) => {
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: new RegExp(mode, "i") }));
  // последствия выбора -- на экране до подтверждения
  expect(screen.getByTestId("update-modes").textContent).toMatch(phrase);
  fireEvent.click(screen.getByRole("button", { name: /^продолжить$/i }));
  fireEvent.click(await screen.findByRole("button", { name: /^подтвердить$/i }));
};

test("the card offers update with three explicit modes, no silent default", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await openBox();
  for (const m of ["продолжить как было", "свежий клон, тот же origin",
                   "другой origin"]) {
    expect(screen.getByRole("radio", { name: new RegExp(m, "i") })).toBeInTheDocument();
  }
  // ни один режим не выбран заранее: выбора «по умолчанию» нет
  const checked = screen.getAllByRole("radio").filter((r) => (r as HTMLInputElement).checked);
  expect(checked).toHaveLength(0);
});

test("keep announces: conversation continues, nothing dies", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: /как было/i }));
  expect(screen.getByTestId("update-modes").textContent)
    .toMatch(/разговор продолж/i);
  expect(screen.getByTestId("update-modes").textContent).toMatch(/ничего не гибнет|несохранённ/);
});

test("replace announces: fresh clone, the conversation dies", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: /свежий клон/i }));
  const t = screen.getByTestId("update-modes").textContent;
  expect(t).toMatch(/свежий клон/i);
  expect(t).toMatch(/разговор (погибнет|умрёт|теряется)/i);
});

test("remove announces the new origin input and its consequences", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: /другой origin/i }));
  // origin -- явное поле: без него подтверждения нет
  expect(screen.getByLabelText(/новый origin/i)).toBeInTheDocument();
  expect(screen.getByTestId("update-modes").textContent)
    .toMatch(/разговор (погибнет|умрёт|теряется)/i);
});

test("confirm shows the exact target before the POST", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: /свежий клон/i }));
  fireEvent.click(screen.getByRole("button", { name: /^продолжить$/i }));
  const dialog = (await screen.findAllByRole("dialog"))
    .find((d) => d.textContent.includes("подтвердите update"));
  expect(dialog?.textContent).toContain("pu-mop-1");
  expect(dialog?.textContent).toContain("hyper");
  expect(dialog?.textContent).toContain("free (master)");
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

test("keep posts fresh=false", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await ride("как было", /разговор продолж/i);
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  expect(JSON.parse((post?.[1] as { body: string }).body))
    .toEqual({ action: "update", target: "pu-mop-1", params: { fresh: false } });
});

test("replace posts fresh=true with the same origin", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await ride("свежий клон", /разговор/i);
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  expect(JSON.parse((post?.[1] as { body: string }).body))
    .toEqual({ action: "update", target: "pu-mop-1", params: { fresh: true } });
});

test("remove posts fresh=true with the new origin", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: /другой origin/i }));
  fireEvent.change(screen.getByLabelText(/новый origin/i),
                   { target: { value: "https://x/y/other.git" } });
  fireEvent.click(screen.getByRole("button", { name: /^продолжить$/i }));
  fireEvent.click(await screen.findByRole("button", { name: /^подтвердить$/i }));
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  expect(JSON.parse((post?.[1] as { body: string }).body))
    .toEqual({ action: "update", target: "pu-mop-1",
               params: { fresh: true, origin: "https://x/y/other.git" } });
});

test("remove without an origin filled posts nothing", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: /другой origin/i }));
  // пустой origin: до подтверждения не дойти -- продолжить заперта
  expect(screen.getByRole("button", { name: /^продолжить$/i })).toBeDisabled();
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(0);
});

test("double submit rides one POST", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await openBox();
  fireEvent.click(screen.getByRole("radio", { name: /свежий клон/i }));
  fireEvent.click(screen.getByRole("button", { name: /^продолжить$/i }));
  const btn = await screen.findByRole("button", { name: /^подтвердить$/i });
  fireEvent.click(btn);
  fireEvent.click(btn);
  await act(async () => { release?.(ok202("op-9")); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("unknown is named unknown, a refusal names the server phrase", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<UpdateBox session={ADMIN} row={ROW} />);
  await ride("свежий клон", /разговор/i);
  await waitFor(() => expect(screen.getByTestId("update-verdict"))
    .toHaveTextContent(/исход неизвестен/i));
  expect(screen.getByTestId("update-verdict")).not.toHaveTextContent(/не удался/);
});

test("a guest and a user see no update button", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<UpdateBox session={{ login: null, role: null }} row={ROW} />);
  expect(screen.queryByRole("button", { name: /правка/i })).toBeNull();
});
