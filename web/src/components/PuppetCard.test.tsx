// Карточка папета (#463): приватная панель админа -- факты строки ростера
// (#452: kind/detail/branch как в таблице, тот же formatter) и хвост экрана
// по явному запросу. Клавиатурно доступная, ANSI/разметка не исполняются,
// ничего не пишется в storage.
//
// RESULT: тесты написаны до реализации (RED).
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PuppetCard } from "./PuppetCard";
import { emptySnapshot, renderUi } from "../test-utils";
import type { Puppet, Snapshot } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };
const GUEST_SESS: Session = { login: null, role: null };

const CARD: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running", state: "free (master)",
  kind: "free", bucket: "free", detail: null, branch: "master",
  owner: "-", llm: "-", origin: "git@h:g/mop.git", disk_kb: null,
};

const snap = (p: Puppet): Snapshot => emptySnapshot({
  projects: [{ name: "mop", counts: {
    puppets: 1, free: 1, busy: 0, sick: 0, silent: 0, down: 0,
  }, puppets: [p] }],
});

const cardOk = (puppet: Puppet) =>
  ({ ok: true, status: 200, json: async () => ({ puppet }) }) as Response;
const tailOk = (lines: string[], truncated = false) =>
  ({ ok: true, status: 200, json: async () => ({ lines, truncated }) }) as Response;

const open = (p: Puppet = CARD) => {
  renderUi(<PuppetCard snapshot={snap(p)} session={ADMIN} target={p.name} onClose={() => {}} />);
};

afterEach(() => vi.unstubAllGlobals());

test("the card shows the roster facts with the shared formatter", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => cardOk({ ...CARD, detail: "not responding" })));
  open();
  // слово статуса -- из verdictWord, не своя копия (#463: не второй enum)
  await waitFor(() => expect(screen.getByText("свободен")).toBeInTheDocument());
  expect(screen.getByText("master")).toBeInTheDocument();
  expect(screen.getByText("not responding")).toBeInTheDocument();
  expect(screen.getByText(/hyper/)).toBeInTheDocument();
  expect(screen.getByText("git@h:g/mop.git")).toBeInTheDocument();
});

test("unknown facts are explained, not guessed", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => cardOk(CARD)));
  open();
  await waitFor(() => expect(screen.getByText("свободен")).toBeInTheDocument());
  // disk_kb null -- «-», модель null -- «-», владелец без аренды -- «-»
  const dashes = await screen.findAllByText("-");
  expect(dashes.length).toBeGreaterThanOrEqual(3);
});

test("the card is keyboard accessible and closes from the keyboard", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => cardOk(CARD)));
  const onClose = vi.fn();
  renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name} onClose={onClose} />);
  const closer = await screen.findByRole("button", { name: /закрыть/i });
  closer.focus();
  expect(document.activeElement).toBe(closer);
  // Mantine закрывает по клику (кнопка) и по Escape (модал) -- Enter на
  // нативной кнопке тоже жмёт её; проверяем доступный путь закрытия
  fireEvent.click(closer);
  expect(onClose).toHaveBeenCalled();
});

test("the tail loads only on request, truncation is named", async () => {
  const fetchMock = vi.fn(async (url: string) =>
    String(url).endsWith("/tail") ? tailOk(["line 1", "line 2"], true) : cardOk(CARD));
  vi.stubGlobal("fetch", fetchMock);
  open();
  // до запроса -- ни строк, ни обращения к /tail
  await waitFor(() => expect(screen.getByText("свободен")).toBeInTheDocument());
  expect(fetchMock).toHaveBeenCalledTimes(1);   // карточка, не хвост
  expect(screen.queryByTestId("tail-lines")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: /экран/i }));
  await waitFor(() => expect(screen.getByTestId("tail-lines")).toBeInTheDocument());
  expect(screen.getByText("line 1")).toBeInTheDocument();
  expect(screen.getByText("обрезано", { exact: false })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(fetchMock).toHaveBeenLastCalledWith("/api/puppet/pu-mop-1/tail",
    expect.objectContaining({ credentials: "same-origin" }));
  // повторный запрос -- снова кнопка, без фонового follow
  fireEvent.click(screen.getByRole("button", { name: /обновить/i }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
});

test("screen text with markup is shown as text, never executed", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) =>
    String(url).endsWith("/tail")
      ? tailOk(['<img src=x onerror="alert(1)">', "\x1b[31mred ansi\x1b[0m",
                 "<script>alert(2)</script>"])
      : cardOk(CARD)));
  open();
  await waitFor(() => expect(screen.getByText("свободен")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: /экран/i }));
  await waitFor(() => expect(screen.getByTestId("tail-lines")).toBeInTheDocument());
  expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
  expect(screen.getByText("\x1b[31mred ansi\x1b[0m")).toBeInTheDocument();
  expect(document.querySelector("img[src=x]")).toBeNull();
  expect(document.querySelector("script")).toBeNull();
});

test("a guest sees no card even with a target", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PuppetCard snapshot={snap(CARD)} session={GUEST_SESS} target={CARD.name} onClose={() => {}} />);
  expect(screen.queryByText(CARD.name)).toBeNull();
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

test("403 on the card: no facts on the screen", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 403 }) as Response));
  open();
  await waitFor(() => expect(screen.getByText(/прав не хватает/)).toBeInTheDocument());
  expect(screen.queryByText("master")).toBeNull();
  expect(screen.queryByRole("tab", { name: /экран/i })).toBeNull();
});

test("an unknown target names the refusal, the close button stays", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 404 }) as Response));
  open();
  await waitFor(() => expect(screen.getByText(/нет такого папета/)).toBeInTheDocument());
  expect(screen.getByRole("button", { name: /закрыть/i })).toBeInTheDocument();
});
