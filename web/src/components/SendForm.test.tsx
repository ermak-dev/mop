// #469: адресный send, один POST, payload не в URL/storage, честный исход.
// #489: те же гарантии проверяются через поле в диалоге экрана.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { PoolTable } from "./PoolTable";
import { SendForm, sendWord } from "./SendForm";
import { emptySnapshot, renderUi } from "../test-utils";
import type { Puppet } from "../types";

const notice = vi.hoisted(() => ({ show: vi.fn() }));
vi.mock("@mantine/notifications", () => ({ notifications: { show: notice.show } }));
const ADMIN = { login: "anton", role: "admin" as const };
const CARD: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};
const projects = emptySnapshot({ projects: [{ name: "mop", counts: {
  puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0,
}, puppets: [CARD] }] }).projects;
const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string, error?: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "send",
    target: CARD.name, status, error }) }) as Response;
const route = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: RequestInit) =>
    init?.method === "POST" ? post() : String(url).endsWith("/tail")
      ? ({ ok: true, json: async () => ({ lines: ["screen"] }) }) as Response : get());
const open = () => {
  const view = renderUi(<PoolTable projects={projects} session={ADMIN} />);
  fireEvent.click(screen.getByRole("button", { name: "Экран" }));
  return view;
};
const typeAndSend = async () => {
  fireEvent.change(await screen.findByLabelText("Сообщение"), { target: { value: "привет" } });
  fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
};
const posts = (mock: ReturnType<typeof route>) => mock.mock.calls.filter((c) => c[1]?.method === "POST");

beforeEach(() => notice.show.mockClear());
afterEach(() => vi.unstubAllGlobals());

const readyForNext = async () => {
  fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "следующее" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled());
  expect(screen.queryByTestId("send-verdict")).toBeNull();
  expect(notice.show).not.toHaveBeenCalled();
};

test("sendWord names the outcome for a human", () => {
  expect(sendWord("succeeded")).toBe("Доставлено");
  expect(sendWord("refused")).toBe("Отказано");
  expect(sendWord("unknown")).toBe("Исход неизвестен");
  expect(sendWord("running")).toBe("Идёт");
});

test("#469 the row offers send to the admin and posts the narrow schema", async () => {
  const fetchMock = route(async () => ok202("op-1"), async () => opJson("succeeded"));
  vi.stubGlobal("fetch", fetchMock);
  open();
  await typeAndSend();
  await readyForNext();
  const post = posts(fetchMock)[0];
  expect(post[0]).toBe("/api/admin/ops");
  expect(JSON.parse(post[1]!.body as string))
    .toEqual({ action: "send", target: CARD.name, params: { message: "привет" } });
});

test("#469 the payload never rides the URL or storage", async () => {
  const fetchMock = route(async () => ok202("op-1"), async () => opJson("succeeded"));
  vi.stubGlobal("fetch", fetchMock);
  open();
  await typeAndSend();
  await readyForNext();
  for (const call of fetchMock.mock.calls) expect(String(call[0])).not.toContain("привет");
  expect(JSON.stringify(window.localStorage)).not.toContain("привет");
  expect(JSON.stringify(window.sessionStorage)).not.toContain("привет");
  expect(window.location.search).toBe("");
});

test("#469 double submit is one POST", async () => {
  let release!: (r: Response) => void;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = route(() => slow, async () => opJson("running"));
  vi.stubGlobal("fetch", fetchMock);
  open();
  fireEvent.change(await screen.findByLabelText("Сообщение"), { target: { value: "раз" } });
  const button = screen.getByRole("button", { name: "Отправить" });
  fireEvent.click(button);
  fireEvent.click(button);
  await act(async () => { release(ok202("op-1")); });
  expect(posts(fetchMock)).toHaveLength(1);
});

test.each([["refused", "Отказано"], ["unknown", "Исход неизвестен"]])(
  "#469 %s verdict is shown with the server reason", async (status, word) => {
    vi.stubGlobal("fetch", route(async () => ok202("op-1"), async () => opJson(status, "held by ermak")));
    open();
    await typeAndSend();
    await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(1));
    expect(notice.show.mock.calls[0][0].message).toContain(word);
    expect(notice.show.mock.calls[0][0].message).toContain("held by ermak");
    expect(screen.queryByText("held by ermak")).toBeNull();
  });

test("#469 a submit refusal names the phrase without fake success", async () => {
  vi.stubGlobal("fetch", route(async () => ({ ok: false, status: 403,
    json: async () => ({ error: "admin only" }) }) as Response, async () => opJson("running")));
  open();
  await typeAndSend();
  await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(1));
  expect(notice.show.mock.calls[0][0].message).toContain("admin only");
  expect(notice.show.mock.calls[0][0].message).not.toContain("Доставлено");
  expect(screen.queryByText("Доставлено")).toBeNull();
});

test("#469 a guest sees no send form or icon", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PoolTable projects={projects} session={{ login: null, role: null }} />);
  expect(screen.queryByLabelText("Сообщение")).toBeNull();
  expect(screen.queryByRole("button", { name: "сообщение" })).toBeNull();
  expect(vi.mocked(fetch)).not.toHaveBeenCalled();
});

test("#469 an empty message posts nothing", async () => {
  const fetchMock = route(async () => ok202("op-1"), async () => opJson("succeeded"));
  vi.stubGlobal("fetch", fetchMock);
  open();
  fireEvent.click(await screen.findByRole("button", { name: "Отправить" }));
  expect(posts(fetchMock)).toHaveLength(0);
});

test.each(["logout", "target"])("#469 late verdict after %s lands nowhere", async (change) => {
  let release!: (r: Response) => void;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = route(() => slow, async () => opJson("succeeded"));
  vi.stubGlobal("fetch", fetchMock);
  const view = open();
  await typeAndSend();
  view.rerender(<MantineProvider><PoolTable projects={change === "target" ? [] : projects}
    session={change === "logout" ? { login: null, role: null } : ADMIN} /></MantineProvider>);
  await act(async () => { release(ok202("op-1")); });
  expect(screen.queryByTestId("send-verdict")).toBeNull();
  expect(notice.show).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(posts(fetchMock)).toHaveLength(1);
});

test("#469 the bare form works standalone", async () => {
  vi.stubGlobal("fetch", route(async () => ok202("op-1"), async () => opJson("succeeded")));
  renderUi(<SendForm session={ADMIN} target={CARD.name} />);
  await typeAndSend();
  await readyForNext();
});
