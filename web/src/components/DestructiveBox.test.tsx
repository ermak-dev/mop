// Разрушительные действия из карточки (#473): recycle/delete/wipe --
// три отдельных действия, каждое двухшаговое: сначала preview последствий
// (driver-specific, из #456/#471), затем подтверждение точной цели (имя,
// узел, состояние на экране). Stale: цель сменилась между preview и
// подтверждением -- подтверждение не уезжает, повтор -- новый preview.
// Частичный исход -- «частично», recovery не обещается. Никакого force.
//
// RESULT: тесты написаны до реализации (RED).
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { DestructiveBox } from "./DestructiveBox";
import { renderUi } from "../test-utils";
import { MantineProvider } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

const ROW: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "busy: #12", kind: "busy", bucket: "busy", detail: null,
  branch: "#12", owner: "ermak", llm: "glm",
  origin: "git@h:g/mop.git", disk_kb: null,
};

const ok202 = (id: string) =>
  ({ ok: true, status: 202, json: async () => ({ id, status: "accepted" }) }) as Response;
const opJson = (status: string, action = "recycle") =>
  ({ ok: true, status: 200, json: async () => ({ id: "op-1", action,
     target: "pu-mop-1", status }) }) as Response;
const postGet = (post: () => Promise<Response>, get: () => Promise<Response>) =>
  vi.fn(async (url: string | URL, init?: { method?: string }) =>
    (init?.method ?? "GET") === "POST" ? post() : get());

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("three separate actions are offered, each with its own preview", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderBox();
  for (const label of ["recycle", "delete", "wipe"]) {
    expect(screen.getByRole("button", { name: new RegExp(label, "i") }))
      .toHaveTextContent(new RegExp(label, "i"));
  }
  // preview recycle: клон начисто, тело живо, несохранённая работа гибнет
  fireEvent.click(screen.getByRole("button", { name: /^recycle$/i }));
  const preview = await screen.findByText(/несохранённая работа погибнет/i);
  expect(preview).toBeInTheDocument();
  // подтверждения ещё нет -- это первый шаг
  expect(screen.queryByRole("button", { name: /^подтвердить$/i })).toBeNull();
});

test("delete preview names clone+job, wipe preview names the body", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderBox();
  fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
  expect(await screen.findByText(/клон и джоб удаляются/i)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /отмена/i }));
  fireEvent.click(screen.getByRole("button", { name: /^wipe$/i }));
  expect(await screen.findByText(/тело пересоздаётся/i)).toBeInTheDocument();
});

test("the second step confirms the exact on-screen target", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderBox();
  fireEvent.click(screen.getByRole("button", { name: /^recycle$/i }));
  await screen.findByText(/несохранённая работа погибнет/i);
  fireEvent.click(screen.getByRole("button", { name: /продолжить/i }));
  // диалог подтверждения ищем по заголовку: карточных диалогов тут нет,
  // но модалей может быть больше одного
  await screen.findByText("подтвердите recycle");
  const dialog = screen.getAllByRole("dialog")
    .find((d) => d.textContent.includes("подтвердите recycle"));
  expect(dialog?.textContent).toContain("pu-mop-1");
  expect(dialog?.textContent).toContain("hyper");
  expect(dialog?.textContent).toContain("busy: #12");
  expect(vi.mocked(globalThis.fetch)).not.toHaveBeenCalled();
});

test("confirm posts the chosen action and the verdict shows", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running", "recycle")));
  vi.stubGlobal("fetch", fetchMock);
  renderBox();
  await rideRecycle();
  await waitFor(() => expect(screen.getByText(/идёт/i)).toBeInTheDocument());
  expect(fetchMock).toHaveBeenCalledWith("/api/admin/ops", expect.objectContaining({
    method: "POST", credentials: "same-origin" }));
  const body = JSON.parse((fetchMock.mock.calls[0][1] as { body: string }).body);
  expect(body).toEqual({ action: "recycle", target: "pu-mop-1", params: {} });
});

test("a target change between preview and confirm kills the confirm", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  const view = renderBox();
  fireEvent.click(screen.getByRole("button", { name: /^recycle$/i }));
  await screen.findByText(/несохранённая работа погибнет/i);
  fireEvent.click(screen.getByRole("button", { name: /продолжить/i }));
  await screen.findByText("подтвердите recycle");
  // цель сменилась под открытым потоком (строка перерисовалась другой):
  // rerender -- под тем же провайдером, что первый рендер
  await act(async () => {
    view.rerender(<MantineProvider>
      <DestructiveBox session={ADMIN} row={{ ...ROW, name: "pu-mop-2" }} />
    </MantineProvider>);
  });
  // подтверждение уехало вместе с прежней целью: POST не начался
  expect(screen.queryByText("подтвердите recycle")).toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
  // повтор по имени -- не молча ретраит: поток начинается заново, с preview
  await act(async () => {
    view.rerender(<MantineProvider>
      <DestructiveBox session={ADMIN} row={ROW} />
    </MantineProvider>);
  });
  expect(screen.queryByText(/несохранённая работа погибнет/i)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^recycle$/i }));
  expect(await screen.findByText(/несохранённая работа погибнет/i)).toBeInTheDocument();
});

test("partial is named partial, no recovery is promised", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("partial")));
  vi.stubGlobal("fetch", fetchMock);
  renderBox();
  await rideRecycle();
  await waitFor(() => expect(screen.getByText(/частично/i)).toBeInTheDocument());
  expect(screen.queryByText(/восстанов/i)).toBeNull();
});

test("double submit rides one POST", async () => {
  let release: ((r: Response) => void) | null = null;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const fetchMock = postGet(() => slow, () => Promise.resolve(opJson("running")));
  vi.stubGlobal("fetch", fetchMock);
  renderBox();
  fireEvent.click(screen.getByRole("button", { name: /^recycle$/i }));
  await screen.findByText(/несохранённая работа погибнет/i);
  fireEvent.click(screen.getByRole("button", { name: /продолжить/i }));
  // двойной под: два клика по «подтвердить» -- POST один
  // двойной под: два клика, пока POST в полёте -- POST один
  const confirmBtn = await screen.findByRole("button", { name: /^подтвердить$/i });
  fireEvent.click(confirmBtn);
  fireEvent.click(confirmBtn);
  await act(async () => { release?.(ok202("op-9")); });
  // дать act-очереди опустеть: брошенный submit не подмешивает следующий тест
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(fetchMock.mock.calls.filter((c) => (c[1] as { method?: string })?.method === "POST"))
    .toHaveLength(1);
});

test("unknown is named unknown, never failed", async () => {
  const fetchMock = postGet(() => Promise.resolve(ok202("op-1")),
                            () => Promise.resolve(opJson("unknown", "wipe")));
  vi.stubGlobal("fetch", fetchMock);
  renderBox();
  await waitFor(() => expect(screen.getByRole("button", { name: /^wipe$/i })).toBeInTheDocument());
  await rideWipe();
  await waitFor(() => expect(screen.getByText(/неизвестен/i)).toBeInTheDocument());
  expect(screen.queryByText(/не удался/i)).toBeNull();
});

function renderBox(row: Puppet = ROW, session: Session = ADMIN) {
  return renderUi(<DestructiveBox session={session} row={row} />);
}

/** Пройти recycle до POST: preview -> продолжить -> подтвердить. */
async function rideRecycle() {
  fireEvent.click(screen.getByRole("button", { name: /^recycle$/i }));
  await screen.findByText(/несохранённая работа погибнет/i);
  fireEvent.click(screen.getByRole("button", { name: /продолжить/i }));
  fireEvent.click(await screen.findByRole("button", { name: /^подтвердить$/i }));
}

/** Пройти wipe до POST. */
async function rideWipe() {
  fireEvent.click(screen.getByRole("button", { name: /^wipe$/i }));
  await screen.findByText(/тело пересоздаётся/i);
  fireEvent.click(screen.getByRole("button", { name: /продолжить/i }));
  fireEvent.click(await screen.findByRole("button", { name: /^подтвердить$/i }));
}
