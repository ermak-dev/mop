// #469: адресный send, один POST, payload не в URL/storage, честный исход.
// #661: канал убран из рабочего окна; гарантии SendForm проверяются отдельно.
// #627: тот же контракт через общий store контроллера -- POST /api/v1/messages
// с привязкой к назначению (epoch/incarnation/expected_revision), исход --
// подтверждённая Operation после WS-инвалидации, отказ -- DRF error envelope.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { PoolTable } from "./PoolTable";
import { SendForm, sendWord } from "./SendForm";
import { emptySnapshot, renderUi } from "../test-utils";
import { ADMIN, EPOCH, ID, INCARNATION, deferred, networkFixture, reply } from "../network-test-utils";
import type { Puppet } from "../types";

const notice = vi.hoisted(() => ({ show: vi.fn() }));
vi.mock("@mantine/notifications", () => ({ notifications: { show: notice.show } }));
const CARD: Puppet = {
  name: ID, node: "node", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};
const projects = emptySnapshot({ projects: [{ name: "mop", counts: {
  puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0,
}, puppets: [CARD] }] }).projects;

let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); notice.show.mockClear(); });
afterEach(() => { net.stop(); vi.unstubAllGlobals(); });

const open = async () => { await net.start(); return renderUi(<SendForm session={ADMIN} target={ID} />); };
const typeAndSend = async (text = "привет") => {
  fireEvent.change(await screen.findByLabelText("Сообщение"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
};
/** Подтверждённый исход приходит GET-ом после WS-инвалидации Operation. */
const finish = async (state: string, reason = "") => {
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => String(url).startsWith("/api/v1/operations")
    ? Promise.resolve(reply(net.operation(state, "3", reason))) : base(url, init));
  await net.invalidateOperation();
};
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

test("#469 standalone send posts the controller message command", async () => {
  await open();
  await typeAndSend();
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  await finish("succeeded");
  await readyForNext();
  const [url, init] = net.posts()[0];
  expect(url).toBe("/api/v1/messages");
  expect(JSON.parse(String(init?.body))).toEqual({ schema_version: 1, epoch: EPOCH, request_id: net.requestId,
    incarnation: INCARNATION, expected_revision: "7", target_id: ID, not_after: expect.any(String),
    params: { message: "привет" } });
});

test("#469 the payload never rides the URL or storage", async () => {
  await open();
  await typeAndSend();
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  await finish("succeeded");
  await readyForNext();
  for (const call of net.http.mock.calls) expect(String(call[0])).not.toContain("привет");
  expect(JSON.stringify(window.localStorage)).not.toContain("привет");
  expect(JSON.stringify(window.sessionStorage)).not.toContain("привет");
  expect(window.location.search).toBe("");
});

test("#469 double submit is one POST", async () => {
  await open();
  const slow = deferred(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => {
    if (init?.method === "POST") { net.capture(init); return slow.promise; }
    return base(url, init);
  });
  fireEvent.change(await screen.findByLabelText("Сообщение"), { target: { value: "раз" } });
  const button = screen.getByRole("button", { name: "Отправить" });
  fireEvent.click(button);
  fireEvent.click(button);
  await act(async () => { slow.resolve(reply(net.operation("accepted", "1"), 202)); });
  expect(net.posts()).toHaveLength(1);
});

test.each([["refused", "Отказано"], ["unknown", "Исход неизвестен"]])(
  "#469 %s verdict is shown with the server reason", async (status, word) => {
    await open();
    await typeAndSend();
    await waitFor(() => expect(net.posts()).toHaveLength(1));
    await finish(status, "held by ermak");
    await waitFor(() => expect(notice.show).toHaveBeenCalledTimes(1));
    expect(notice.show.mock.calls[0][0].message).toContain(word);
    expect(notice.show.mock.calls[0][0].message).toContain("held by ermak");
    expect(screen.queryByText("held by ermak")).toBeNull();
  });

test("#469 a submit refusal names the phrase without fake success", async () => {
  await open();
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => init?.method === "POST"
    ? Promise.resolve(reply({ schema_version: 1, error: { code: "access_denied", message: "admin only",
      retryable: false } }, 403)) : base(url, init));
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
  await open();
  fireEvent.click(await screen.findByRole("button", { name: "Отправить" }));
  expect(net.posts()).toHaveLength(0);
});

test.each(["logout", "target"])("#469 late verdict after %s lands nowhere", async (change) => {
  const view = await open();
  const slow = deferred(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => {
    if (init?.method === "POST") { net.capture(init); return slow.promise; }
    return base(url, init);
  });
  await typeAndSend();
  // #661: смена контекста проверяется без удалённой панели канала.
  view.rerender(<MantineProvider><SendForm target={change === "target" ? "00000000-0000-4000-8000-000000000009" : ID}
    session={change === "logout" ? { login: null, role: null } : ADMIN} /></MantineProvider>);
  await act(async () => { slow.resolve(reply(net.operation("succeeded", "1"), 200)); });
  expect(screen.queryByTestId("send-verdict")).toBeNull();
  expect(notice.show).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(net.posts()).toHaveLength(1);
});

test("#469 the bare form works standalone", async () => {
  await open();
  await typeAndSend();
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  await finish("succeeded");
  await readyForNext();
});
