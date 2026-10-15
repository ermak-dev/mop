// HYPOTHESIS: просмотр требует второго клика, а факты отнимают высоту.
// SOLUTION: открытый диалог сразу читает выбранный источник без карточки;
// права, экранирование и отбрасывание старых ответов остаются прежними.
// RESULT: автозагрузка экрана и приватность проверены.
// STATUS: FIXED — see #487
// #507: источник один -- экран; ветка stderr ушла с кнопкой «лог», все
// проверки переписаны под один источник.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { afterEach, expect, test, vi } from "vitest";
import { PuppetCard } from "./PuppetCard";
import { renderUi } from "../test-utils";

// #661: снимок открывается автоматически при инициализации; отдельной кнопки нет.
const ADMIN = { login: "anton", role: "admin" as const };
const tailOk = (lines: string[], truncated = false) =>
  ({ ok: true, status: 200, json: async () => ({ source: 'initialization', lines, truncated }) }) as Response;
const open = () => {
  const fetchTail = vi.mocked(fetch);
  vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
    if (String(url).includes('/history')) return Promise.resolve({ ok: true, status: 200, json: async () => ({ epoch: 'e', session_id: 's', cursor: 'c', records: [], reset: false, partial: false, has_more: false }) });
    if (String(url).includes('/read?')) return new Promise<Response>(() => {});
    if (String(url).endsWith('/close')) return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) } as Response);
    return fetchTail(url, init);
  });
  const view = renderUi(<PuppetCard session={ADMIN} target="pu-mop-1" onClose={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  return view;
};

afterEach(() => vi.unstubAllGlobals());

test("#622 opening the snapshot loads only its private tail endpoint", async () => {
  const fetchMock = vi.fn(async () => tailOk(["line 1", "line 2"], true));
  vi.stubGlobal("fetch", fetchMock);
  open();
  await screen.findByText("line 1");
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith("/api/puppet/pu-mop-1/tail",
    expect.objectContaining({ credentials: "same-origin" }));
  expect(screen.getByText(/обрезано/)).toBeInTheDocument();
  for (const label of ["узел", "ветка", "владелец", "модель", "репозиторий", "место клона", "state-строка"])
    expect(screen.queryByText(label, { exact: true })).toBeNull();
  // #622: чат открывается первым; снимок доступен внутри единственного терминала.
  expect(screen.getAllByRole("tab").map(tab => tab.textContent)).toEqual(["Чат", "Терминал"]);
  expect(screen.getByRole("tab", { name: "Терминал" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("button", { name: "Обновить" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
});

test("viewer has an accessible close button", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => tailOk([])));
  const onClose = vi.fn();
  renderUi(<PuppetCard session={ADMIN} target="pu-mop-1" onClose={onClose} />);
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  const closer = screen.getByRole("button", { name: "Закрыть" });
  closer.focus();
  expect(document.activeElement).toBe(closer);
  fireEvent.click(closer);
  expect(onClose).toHaveBeenCalled();
  await screen.findByText("вывод пуст");
});

test("screen markup and ANSI remain text", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => tailOk([
    '<img src=x onerror="alert(1)">', "\x1b[31mred ansi\x1b[0m", "<script>alert(2)</script>",
  ])));
  open();
  expect(await screen.findByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
  expect(screen.getByText("\x1b[31mred ansi\x1b[0m")).toBeInTheDocument();
  expect(document.querySelector("img[src=x]")).toBeNull();
  expect(document.querySelector("script")).toBeNull();
});

test("a guest cannot open or request the screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PuppetCard session={{ login: null, role: null }} target="pu-mop-1"
    onClose={() => {}} />);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(vi.mocked(fetch)).not.toHaveBeenCalled();
});

test.each([[403, "прав не хватает"], [404, "нет такого папета"], [502, "панель не отвечает"]])(
  "HTTP %s stops the spinner and displays the refusal", async (status, message) => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status }) as Response));
    open();
    expect(await screen.findByRole("alert")).toHaveTextContent(message as string);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("button", { name: "Закрыть" })).toBeInTheDocument();
  });

test("#487 switching sources never displays the old response", async () => {
  let release!: (r: Response) => void;
  const slow = new Promise<Response>((resolve) => { release = resolve; });
  const mock = vi.fn().mockImplementationOnce(async () => slow)
    .mockResolvedValue(tailOk(["new screen"]));
  vi.stubGlobal("fetch", mock);
  const view = open();
  await waitFor(() => expect(mock).toHaveBeenCalled());
  view.rerender(<MantineProvider><PuppetCard session={ADMIN} target="pu-mop-2" onClose={() => {}} /></MantineProvider>);
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  await screen.findByText("new screen");
  await act(async () => { release(tailOk(["old screen"])); });
  expect(screen.queryByText("old screen")).toBeNull();
  expect(screen.getByText("new screen")).toBeInTheDocument();
});

// #507/#622: снимок tmux доступен внутри одного терминала, запроса stderr нет.
test("#622 the terminal offers a snapshot but no second log dialog or stderr request", async () => {
  const seen: string[] = [];
  const fetchMock = vi.fn(async (url: string | URL | Request) => {
    seen.push(String(url));
    return tailOk(["screen"]);
  });
  vi.stubGlobal("fetch", fetchMock);
  open();
  await screen.findByText("screen");
  expect(screen.getByRole("complementary", { name: "экран pu-mop-1" })).toBeInTheDocument();
  // @ts-expect-error #507: пропа which у карточки больше нет
  renderUi(<PuppetCard session={ADMIN} target="pu-mop-1" which="stderr" onClose={() => {}} />);
  expect(screen.queryByRole("dialog", { name: /лог/ })).toBeNull();
  expect(seen.every((url) => !url.includes("/stderr"))).toBe(true);
});
