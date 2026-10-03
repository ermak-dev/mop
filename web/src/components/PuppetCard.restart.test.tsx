// Restart живёт в карточке папета (#465): кнопка видна админу в карточке,
// гость/юзер её не видят вовсе. Смена цели между подтверждением и кликом
// подтверждает уже не ту строку: stale-подтверждение не уезжает.
//
// RESULT: тесты написаны до реализации (RED).
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PuppetCard } from "./PuppetCard";
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
const opJson = (status: string) =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action: "restart",
     target: "pu-mop-1", status }) }) as Response;

afterEach(() => vi.unstubAllGlobals());

test("the admin sees the restart button in the card, it opens the confirm", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => cardOk(CARD)));
  renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN} target={CARD.name}
                       onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText("свободен")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /рестарт/i }));
  // диалога два: сама карточка и подтверждение -- берём подтверждение
  const dialog = (await screen.findAllByRole("dialog"))
    .find((d) => d.textContent.includes("подтвердите рестарт"));
  expect(dialog?.textContent).toContain("pu-mop-1");
  expect(dialog?.textContent).toContain("hyper");
  expect(dialog?.textContent).toContain("free (master)");
});

test("a guest never sees the restart button", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PuppetCard snapshot={snap(CARD)} session={{ login: null, role: null }}
                       target={CARD.name} onClose={() => {}} />);
  expect(screen.queryByRole("button", { name: /рестарт/i })).toBeNull();
});

test("stale confirm after a target switch rides nowhere", async () => {
  // подтверждение открыто для pu-mop-1; цель сменили -- подтверждение
  // уже не про ту строку: подтверждение закрывается, POST не уходит
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  // GET маршрутизуется по URL: карточка -- карточкой, результат -- результатом
  const fetchMock = vi.fn(async (url: string | URL, init?: { method?: string }) => {
    const u = String(url);
    if ((init?.method ?? "GET") === "POST") return slow;
    if (u.includes("/api/admin/ops")) return opJson("running");
    return cardOk(CARD);
  });
  vi.stubGlobal("fetch", fetchMock);
  const view = renderUi(<PuppetCard snapshot={snap(CARD)} session={ADMIN}
                                    target={CARD.name} onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText("свободен")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /рестарт/i }));
  await screen.findByText("подтвердите рестарт");
  // цель сменилась под открытым подтверждением
  await act(async () => {
    view.rerender(<PuppetCard snapshot={snap(CARD)} session={ADMIN}
                              target={null} onClose={() => {}} />);
  });
  // карточка закрылась -- подтверждение вместе с ней; подтверждать нечего
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => { release?.(ok202("op-x")); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(0);
});

