// HYPOTHESIS: App читает projects и рисует masters отдельной секцией;
// канонический sessions игнорируется, роли и фильтр отсутствуют.
// SOLUTION: один ростер с явным различием managed/local и фильтром роли.
// RESULT: прежний App дал восемь отказов; общий ростер проходит их.
// STATUS: FIXED — see #588.
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { vi } from "vitest";
import App from "./App";
import { emptySnapshot, renderUi } from "./test-utils";
import type { Puppet, Snapshot } from "./types";
import type { Session } from "./useSession";

const pool = vi.hoisted(() => ({ snapshot: null as Snapshot | null }));
const auth = vi.hoisted(() => ({ current: { login: null, role: null } as Session }));
vi.mock("./usePool", () => ({ usePool: () => pool }));
vi.mock("./useSession", async (original) => ({
  ...await original<typeof import("./useSession")>(),
  fetchSession: () => Promise.resolve(auth.current),
}));

const puppet = (name: string): Puppet => ({
  name, node: "node-a", alloc_status: "running", state: "free", kind: "free",
  bucket: "free", branch: "main", owner: "alice", llm: "glm-5.3",
  origin: "git@example.invalid:team/sample.git", disk_kb: null,
});
const managed = (name: string, role: string) => {
  const { owner, ...fields } = puppet(name);
  return { ...fields, source: "managed" as const, id: `${name}@instance`, project: "sample",
    role, role_source: "meta" as const, user: owner, unmanaged: false as const,
    conversation: null, addresses: [name], soul_digest: "a".repeat(64), incarnation: "instance" };
};
const local = (id: string, address: string) => ({
  source: "local" as const, id, project: "sample", user: "alice", role: null,
  role_source: "legacy" as const, unmanaged: true as const, conversation: "same name",
  display: "same name", cwd: "/workspace/sample", addresses: [address], identified: true,
});
const legacyMaster = (address: string) => ({
  project: "sample", master: address, addresses: [address], user: "alice",
  session: "same name", cwd: "/workspace/sample", identified: true, puppets: ["pu-sample-1"],
});
function snapshot() {
  return { ...emptySnapshot({
    counts: { puppets: 3, free: 3, busy: 0, sick: 0, silent: 0, down: 0 },
    projects: [{ name: "sample", counts: { puppets: 3, free: 3, busy: 0, sick: 0, silent: 0, down: 0 },
      puppets: [puppet("pu-sample-1")] }],
    masters: [legacyMaster("alice.host-1"), legacyMaster("alice.host-2")],
  }), sessions: [managed("pu-sample-1", "puppet"), managed("pu-sample-2", "master"),
    managed("pu-sample-3", "builder"), local("local-one", "alice.host-1"),
    local("local-two", "alice.host-2")] };
}
beforeEach(() => {
  auth.current = { login: null, role: null };
  pool.snapshot = snapshot();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200,
    json: async () => ({ lines: [], truncated: false }) })));
});
afterEach(() => vi.unstubAllGlobals());

async function mount() {
  let view: ReturnType<typeof renderUi> | undefined;
  await act(async () => { view = renderUi(<App />); });
  return view!;
}
const rows = () => within(screen.getByTestId("pool")).getAllByRole("row").filter(row => row.hasAttribute('data-source'));

test("#588 canonical sessions form one roster, without a separate masters section", async () => {
  await mount();
  expect(screen.queryByTestId("masters")).not.toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "Role" })).toBeInTheDocument();
  expect(rows()).toHaveLength(5);
  const roster = within(screen.getByTestId("pool"));
  expect(roster.getByText("pu-sample-2")).toBeInTheDocument();
  expect(roster.getByText("pu-sample-3")).toBeInTheDocument();
  expect(roster.getAllByText("same name")).toHaveLength(2);
});

test("#619 canonical pending session displays the placement reason", async () => {
  pool.snapshot = { ...snapshot(), sessions: [{ ...managed("pu-sample-9", "builder"),
    node: "-", alloc_status: "pending", kind: "queued", bucket: "down",
    state: "queued: no free slots in the pool", detail: "no free slots in the pool" }] };
  await mount();
  const pending = within(screen.getByTestId("pool"));
  expect(pending.getByText("ожидает размещения")).toBeInTheDocument();
  expect(pending.getByText("no free slots in the pool")).toBeInTheDocument();
  expect(pending.queryByText("поднимается")).not.toBeInTheDocument();
});

test("#664 project filter changes visible entries, not capacity counters", async () => {
  const next = snapshot();
  next.sessions.push({ ...managed('pu-other-1', 'puppet'), project: 'other' });
  pool.snapshot = next;
  await mount();
  const counts = screen.getByTestId("chips").textContent;
  expect(screen.queryByRole('combobox', { name: 'Role' })).toBeNull();
  fireEvent.change(screen.getByRole("combobox", { name: "Проект" }), { target: { value: "other" } });
  expect(rows()).toHaveLength(1);
  expect(within(rows()[0]).getByText("pu-other-1")).toBeInTheDocument();
  expect(screen.getByTestId("chips").textContent).toBe(counts);
  fireEvent.change(screen.getByRole("combobox", { name: "Проект" }), { target: { value: "" } });
  expect(rows()).toHaveLength(6);
});

test("#588 unmanaged locals have no Nomad actions even for an admin viewer", async () => {
  auth.current = { login: "viewer", role: "admin" };
  await mount();
  await screen.findByText("viewer");
  const localRows = rows().filter((row) => within(row).queryByText("same name"));
  expect(localRows).toHaveLength(2);
  for (const row of localRows) expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  const worker = rows().find((row) => within(row).queryByText("pu-sample-1"))!;
  expect(within(worker).getAllByRole("button").length).toBeGreaterThan(0);
});

test("#588 a master role does not grant an ordinary viewer management controls", async () => {
  auth.current = { login: "reader", role: "user" };
  await mount();
  await screen.findByText("reader");
  const master = rows().find((row) => within(row).queryByText("pu-sample-2"))!;
  expect(within(master).queryByRole("button")).not.toBeInTheDocument();
});

test("#588 a newer empty canonical roster does not resurrect compatibility projections", async () => {
  pool.snapshot = { ...snapshot(), sessions: [] };
  await mount();
  expect(screen.queryByText("pu-sample-1")).not.toBeInTheDocument();
  expect(screen.queryByText("same name")).not.toBeInTheDocument();
  expect(screen.getByText("No sessions")).toBeInTheDocument();
});

test("#588 old snapshots keep workers and local endpoints in the same roster", async () => {
  const old: Snapshot = snapshot();
  delete old.sessions;
  pool.snapshot = old;
  await mount();
  expect(screen.queryByTestId("masters")).not.toBeInTheDocument();
  expect(rows()).toHaveLength(3);
  expect(within(screen.getByTestId("pool")).getAllByText("same name")).toHaveLength(2);
});

test("#588 refresh replaces rows by stable identity rather than duplicating endpoints", async () => {
  const view = await mount();
  const next = snapshot();
  next.sessions[3] = { ...local("local-one", "alice.new-process"), addresses: ["alice.new-process", "alice.second-endpoint"] };
  pool.snapshot = next;
  view.rerender(<MantineProvider><App /></MantineProvider>);
  expect(rows()).toHaveLength(5);
  expect(screen.queryByText("alice.host-1")).not.toBeInTheDocument();
  const updated = screen.getByText("alice.new-process").closest("tr")!;
  expect(within(updated).getByText("alice.second-endpoint")).toBeInTheDocument();
});

test("#588 legacy endpoints stay unconfirmed and do not acquire the user's workers", async () => {
  pool.snapshot = emptySnapshot({ masters: [{ ...legacyMaster("alice.old-process"),
    addresses: undefined, identified: undefined, puppets: ["pu-sample-9"] }] });
  await mount();
  expect(rows()).toHaveLength(1);
  expect(within(rows()[0]).getByText("MCP endpoint")).toBeInTheDocument();
  expect(within(rows()[0]).getByText("alice.old-process")).toBeInTheDocument();
  expect(screen.queryByText("pu-sample-9")).not.toBeInTheDocument();
});

test("#588 a new declarative role needs no UI-specific enum", async () => {
  const next = snapshot();
  next.sessions.push(managed("pu-sample-4", "reviewer"));
  pool.snapshot = next;
  await mount();
  expect(rows()).toHaveLength(6);
  const reviewer = rows().find(row => within(row).queryByText('pu-sample-4'))!;
  expect(within(reviewer).getByText('reviewer')).toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: 'Role' })).toBeNull();
});

test("#588 the common managed screen identifies its role", async () => {
  auth.current = { login: "viewer", role: "admin" };
  await mount();
  await screen.findByText("viewer");
  fireEvent.click(screen.getByRole("button", { name: "pu-sample-2" }));
  await waitFor(() => expect(screen.getByTestId("puppet-card")).toBeInTheDocument());
  const card = screen.getByTestId("puppet-card");
  expect(within(card).queryByText("Role: master")).toBeNull();
  fireEvent.click(within(card).getByRole("checkbox", { name: "Отображать метаданные" }));
  expect(within(card).getByText("Role: master")).toBeInTheDocument();
});

// #664: клик по любой ячейке заменил отдельную иконку экрана;
// метка роли по-прежнему передаётся в метаданные панели.
test("#664 the row selection keeps the role in the screen panel", async () => {
  auth.current = { login: "viewer", role: "admin" };
  await mount();
  await screen.findByText("viewer");
  const row = screen.getByRole('button', { name: 'pu-sample-2' }).closest('tr')!;
  fireEvent.click(within(row).getByText('node-a'));
  const card = await screen.findByTestId("puppet-card");
  expect(within(card).queryByText("Role: master")).toBeNull();
  fireEvent.click(within(card).getByRole("checkbox", { name: "Отображать метаданные" }));
  expect(within(card).getByText("Role: master")).toBeInTheDocument();
  expect(within(card).getByRole("tab", { name: "Чат" })).toHaveAttribute("aria-selected", "true");
});
