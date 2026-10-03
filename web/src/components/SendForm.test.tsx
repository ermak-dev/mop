// Форма адресного send (#469) в карточке папета: сообщение -> POST
// /api/admin/ops {action: "send", params: {message}} -> вердикт. Политика
// первого выпуска #461: адресная доставка, никакого wait/notify в UI.
// Ход операции -- общий useAdminOp (#465), своих копий очередей и статусов
// у формы нет; payload живёт в поле на время полёта -- в storage и URL
// его нет (запрет storage #463).
//
// RED сценарии приказа: double submit, held/unknown, отказ владельца,
// late response после logout/смены цели, отсутствие payload в URL.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { PuppetCard } from "./PuppetCard";
import { SendForm, sendWord } from "./SendForm";
import { emptySnapshot, renderUi } from "../test-utils";
import type { Puppet, Snapshot } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

const CARD: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "free (master)", kind: "free", bucket: "free", detail: null,
  branch: "master", owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};

const snap = (p: Puppet): Snapshot => emptySnapshot({
  projects: [{ name: "mop", counts: {
    puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0,
  }, puppets: [p] }],
});

const cardOk = (puppet: Puppet) =>
  ({ ok: true, status: 200, json: async () => ({ puppet }) }) as Response;
const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string, error?: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "send",
     target: "pu-mop-1", status, error }) }) as Response;

/** GET маршрутизуется по URL: карточка -- карточкой, результат -- результатом. */
const route = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) => {
    const u = String(url);
    if ((init?.method ?? "GET") === "POST") return post();
    if (u.includes("/api/admin/ops")) return get();
    return cardOk(CARD);
  });

afterEach(() => vi.unstubAllGlobals());

const typeAndSend = async () => {
  fireEvent.change(await screen.findByLabelText("Сообщение"),
                   { target: { value: "привет" } });
  fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
};

test("sendWord names the outcome for a human", () => {
  expect(sendWord("succeeded")).toBe("доставлено");
  expect(sendWord("refused")).toBe("отказано");
  expect(sendWord("unknown")).toBe("исход неизвестен");
  expect(sendWord("running")).toBe("идёт");
});

test("#469 the card offers the send form to the admin, posts the narrow schema", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name}
                       onClose={() => {}} />);
  await typeAndSend();
  await waitFor(() => expect(screen.getByTestId("send-verdict")).toHaveTextContent("доставлено"));
  const post = fetchMock.mock.calls.find((c) => (c[1] as { method?: string })?.method === "POST");
  expect(post?.[0]).toBe("/api/admin/ops");
  expect(JSON.parse((post?.[1] as { body: string }).body))
    .toEqual({ action: "send", target: "pu-mop-1", params: { message: "привет" } });
});

test("#469 the payload never rides the URL or storage", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name}
                       onClose={() => {}} />);
  await typeAndSend();
  await waitFor(() => expect(screen.getByTestId("send-verdict")).toHaveTextContent("доставлено"));
  for (const call of fetchMock.mock.calls) {
    expect(String(call[0])).not.toContain("привет");
  }
  expect(JSON.stringify(window.localStorage)).not.toContain("привет");
  expect(JSON.stringify(window.sessionStorage)).not.toContain("привет");
  expect(window.location.search).toBe("");
});

test("#469 double submit is one POST", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = route(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name}
                       onClose={() => {}} />);
  await screen.findByLabelText("Сообщение");
  fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "раз" } });
  const btn = screen.getByRole("button", { name: "Отправить" });
  fireEvent.click(btn);
  fireEvent.click(btn);
  await act(async () => { release?.(ok202("op-1")); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("#469 held and unknown verdicts are shown with the server's reason", async () => {
  for (const [status, word] of [["refused", "отказано"], ["unknown", "исход неизвестен"]] as const) {
    vi.stubGlobal("fetch", route(() => Promise.resolve(ok202("op-1")),
      () => Promise.resolve(opJson(status, "held by ermak"))));
    const view = renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN}
                                      target={CARD.name} onClose={() => {}} />);
    await typeAndSend();
    await waitFor(() => expect(screen.getByTestId("send-verdict")).toHaveTextContent(word));
    // причина -- рядом, фразой сервера как есть
    expect(screen.getByText("held by ermak")).toBeInTheDocument();
    view.unmount();   // следующая итерация -- своя карточка, без чужой на экране
  }
});

test("#469 an owner refusal at submit names the phrase, no fake success", async () => {
  vi.stubGlobal("fetch", route(
    () => Promise.resolve({ ok: false, status: 403,
      json: async () => ({ error: "admin only" }) }) as Response,
    () => Promise.resolve(opJson("running"))));
  renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name}
                       onClose={() => {}} />);
  await typeAndSend();
  await waitFor(() => expect(screen.getByText(/admin only/)).toBeInTheDocument());
  // отказ -- честный; выдуманного успеха («доставлено») нет
  expect(screen.queryByText("доставлено")).not.toBeInTheDocument();
});

test("#469 a guest sees no send form", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => cardOk(CARD)));
  renderUi(<PuppetCard snapshot={snap(CARD)} session={{ login: null, role: null }}
                       target={CARD.name} onClose={() => {}} />);
  // гостю карточка тела не рисует вовсе (#463) -- и формы в ней нет
  await screen.findByTestId("puppet-card");
  expect(screen.queryByLabelText("Сообщение")).toBeNull();
  expect(screen.queryByRole("button", { name: "Отправить" })).toBeNull();
});

test("#469 an empty message posts nothing", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name}
                       onClose={() => {}} />);
  await screen.findByLabelText("Сообщение");
  fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(0);
});

test("#469 a late verdict after logout lands nowhere", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = route(() => slow, () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name}
                                    onClose={() => {}} />);
  await screen.findByLabelText("Сообщение");
  fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "раз" } });
  fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
  // logout под висящим POST: сессия ушла -- карточка уже не для админа
  await act(async () => {
    view.rerender(<MantineProvider><PuppetCard snapshot={snap(CARD)}
      session={{ login: null, role: null }} target={CARD.name} onClose={() => {}} />
    </MantineProvider>);
  });
  await act(async () => { release?.(ok202("op-1")); });
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  // форма гостю не видна -- и вердикта её операции на экране нет
  expect(screen.queryByTestId("send-verdict")).toBeNull();
});

test("#469 a target switch forgets the in-flight operation", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = route(() => slow, () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name}
                                    onClose={() => {}} />);
  await screen.findByLabelText("Сообщение");
  fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "раз" } });
  fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
  await act(async () => {
    view.rerender(<PuppetCard snapshot={snap(CARD)} session={ADMIN}
                              target={null} onClose={() => {}} />);
  });
  await act(async () => { release?.(ok202("op-1")); });
  // карточка закрыта -- и вердикт забытой операции никуда не приземлился
  expect(screen.queryByTestId("send-verdict")).toBeNull();
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("#469 the bare form still works standalone (for tests and the card's landing)", async () => {
  const fetchMock = route(() => Promise.resolve(ok202("op-1")),
                          () => Promise.resolve(opJson("succeeded")));
  vi.stubGlobal("fetch", fetchMock);
  renderUi(<SendForm session={ADMIN} target="pu-mop-1" />);
  await typeAndSend();
  await waitFor(() => expect(screen.getByTestId("send-verdict")).toHaveTextContent("доставлено"));
});
