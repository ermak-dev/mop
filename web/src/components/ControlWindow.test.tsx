// #520: визард «Правка» -- Mantine Stepper: выбор действия -> параметры ->
// подтверждение. Первый экран -- все шесть действий, вложения
// «правка -> правка» нет: пункт update зовётся «обновление». Рестарт --
// без параметров и без подтверждения: клик шлёт POST сразу (#513),
// Stepper не переключается. Обратимые (update keep, модель) -- шаг
// подтверждения пропускается: «Выполнить» шлёт сразу. Необратимые
// (update replace/remove, recycle/delete/wipe) -- единый финальный
// экран: последствия + факты точной цели + одна кнопка «подтвердить»
// (прежде у разрушительных было два экрана: preview #473 и
// подтверждение #465). Гард #473: смена цели под открытым визардом
// (шаг параметров или дальше) сбрасывает к выбору действия.
// Вердикты -- не здесь: балуны #509 и OperationResult не тронуты.
//
// RESULT: тесты написаны до реализации (RED).
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { ControlWindow } from "./ControlWindow";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";
import type { Session } from "../useSession";

const ADMIN: Session = { login: "anton", role: "admin" };

// llm -- из каталога ниже: пункт «текущая» проверяется на шаге модели
const ROW: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "running",
  state: "busy: #12", kind: "busy", bucket: "busy", detail: null,
  branch: "#12", owner: "ermak", llm: "glm-5.3-flash",
  origin: "git@h:g/mop.git", disk_kb: null,
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Открыть визард со шпионами приказа и закрытия. */
const mount = (row: Puppet = ROW) => {
  const onOrder = vi.fn();
  const onClose = vi.fn();
  const view = renderUi(<ControlWindow opened row={row} session={ADMIN}
    onClose={onClose} onOrder={onOrder} />);
  return { view, onOrder, onClose };
};

test("#520 the choice screen lists six actions, update is named обновление", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  const win = screen.getByTestId("control-window");
  for (const label of ["обновление", "модель", "рестарт", "recycle", "delete", "wipe"]) {
    expect(within(win).getByRole("button", { name: label })).toBeInTheDocument();
  }
  // вложения «правка -> правка» нет: дубль имени окна умер
  expect(within(win).queryByRole("button", { name: "правка" })).toBeNull();
  expect(onOrder).not.toHaveBeenCalled();
});

test("#520 picking an item switches the step, the window stays open", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose, onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "обновление" }));
  // окно живо: шаг параметров с тремя режимами #470
  expect(screen.getByTestId("control-window")).toBeInTheDocument();
  for (const m of ["продолжить как было", "свежий клон, тот же origin", "другой origin"]) {
    expect(screen.getByRole("radio", { name: new RegExp(m, "i") })).toBeInTheDocument();
  }
  expect(onClose).not.toHaveBeenCalled();
  expect(onOrder).not.toHaveBeenCalled();
});

test("#520 the restart item posts at once, the stepper never switches", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose, onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "рестарт" }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "restart", params: {} }));
  expect(onClose).toHaveBeenCalled();
  // никаких шагов: ни радио, ни подтверждения
  expect(screen.queryByRole("radio")).toBeNull();
  expect(screen.queryByRole("button", { name: /подтвердить/i })).toBeNull();
});

// Дизейбл-причина #508 -- на пункте первого экрана: признак строки
// виден до клика, отказ после клика был бы вторым ответом.
test.each([
  ["down", "рестарт недоступен: не запущен"],
  ["failing", "рестарт недоступен: задача падает"],
])("#520 the restart item carries the %s disable reason", (kind, reason) => {
  vi.stubGlobal("fetch", vi.fn());
  const row: Puppet = kind === "down"
    ? { ...ROW, alloc_status: "stopped", bucket: "down", state: "stopped" }
    : { ...ROW, kind: "failing", state: "FAILED: exit 1", detail: "exit 1" };
  const { onOrder } = mount(row);
  const item = screen.getByRole("button", { name: new RegExp(reason) });
  expect(item).toBeDisabled();
  expect(item).toHaveAttribute("aria-label", reason);
  expect(onOrder).not.toHaveBeenCalled();
});

// keep обратим (#513): шаг подтверждения пропускается -- «Выполнить»
// на шаге параметров шлёт приказ сразу.
test("#520 update keep rides Выполнить on the params step, no confirm screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onClose, onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /как было/i }));
  fireEvent.click(screen.getByRole("button", { name: /^выполнить$/i }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "update", params: { fresh: false } }));
  expect(onClose).toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: /подтвердить/i })).toBeNull();
});

// replace необратим: единый финальный экран -- последствия И факты
// точной цели одной страницей, одна кнопка.
test("#520 update replace rides the single confirm screen with target facts", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /свежий клон/i }));
  fireEvent.click(screen.getByRole("button", { name: /^далее$/i }));
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/несохранённая работа погибнет/);
  expect(win.textContent).toContain("pu-mop-1");
  expect(win.textContent).toContain("hyper");
  expect(win.textContent).toContain("busy: #12");
  expect(within(win).getAllByRole("button", { name: /^подтвердить$/i })).toHaveLength(1);
  expect(onOrder).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /^подтвердить$/i }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "update", params: { fresh: true } }));
});

test("#520 update remove requires the origin and posts it", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /другой origin/i }));
  // пустой origin запирает путь дальше: пересадка без адреса не едет
  expect(screen.getByRole("button", { name: /^далее$/i })).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/новый origin/i),
    { target: { value: "https://x/y/other.git" } });
  fireEvent.click(screen.getByRole("button", { name: /^далее$/i }));
  expect(screen.getByTestId("control-window").textContent)
    .toContain("https://x/y/other.git");
  fireEvent.click(screen.getByRole("button", { name: /^подтвердить$/i }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "update", params: { fresh: true, origin: "https://x/y/other.git" } }));
});

// Каталог -- единственный источник (#468): визард спрашивает сервер,
// текущая модель мечена и заперта, без выбора «Выполнить» заперт.
test("#520 model rides the catalog, the current model is locked, Выполнить posts", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200,
    json: async () => ({ models: ["glm-5.3", "glm-5.3-flash"] }) }) as Response));
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "модель" }));
  const radio = await screen.findByRole("radio", { name: /^glm-5\.3$/i });
  expect(screen.getByRole("radio", { name: /текущая/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /^выполнить$/i })).toBeDisabled();
  fireEvent.click(radio);
  fireEvent.click(screen.getByRole("button", { name: /^выполнить$/i }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "model", params: { model: "glm-5.3" } }));
  // смена модели обратима (#513): экрана подтверждения нет
  expect(screen.queryByRole("button", { name: /подтвердить/i })).toBeNull();
});

// Разрушительное -- ONE экран после выбора: последствия и факты цели
// вместе, «продолжить» нет, одна «подтвердить» (прежде -- два экрана).
test("#520 destructive rides ONE consequences screen with a single button", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "recycle" }));
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toMatch(/несохранённая работа погибнет/);
  expect(win.textContent).toMatch(/сила \(force\) не применяется/);
  expect(win.textContent).toContain("pu-mop-1");
  expect(win.textContent).toContain("hyper");
  expect(win.textContent).toContain("busy: #12");
  expect(screen.queryByRole("button", { name: /продолжить/i })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^подтвердить$/i }));
  expect(onOrder).toHaveBeenCalledWith(expect.objectContaining(
    { act: "recycle", params: {} }));
});

test("#520 back walks from confirm to params to choice", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  fireEvent.click(screen.getByRole("button", { name: "обновление" }));
  fireEvent.click(screen.getByRole("radio", { name: /свежий клон/i }));
  fireEvent.click(screen.getByRole("button", { name: /^далее$/i }));
  fireEvent.click(screen.getByRole("button", { name: /^назад$/i }));
  // параметры снова: выбор режима на месте
  expect(screen.getByRole("radio", { name: /свежий клон/i })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^назад$/i }));
  // возврат к выбору действия: все шесть пунктов
  for (const label of ["обновление", "модель", "рестарт", "recycle", "delete", "wipe"]) {
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  }
});

test("#520 destructive back returns straight to the choice screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  mount();
  fireEvent.click(screen.getByRole("button", { name: "wipe" }));
  expect(screen.getByTestId("control-window").textContent).toMatch(/тело пересоздаётся/);
  fireEvent.click(screen.getByRole("button", { name: /^назад$/i }));
  expect(screen.getByRole("button", { name: "recycle" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^подтвердить$/i })).toBeNull();
});

// Гард #473 в визарде: смена цели под открытым окном (шаг параметров
// или дальше) сбрасывает к выбору -- подтверждение умирает, повтор
// начинается с выбора действия, молча ретраить по имени нельзя.
test("#520 a target change under an open wizard resets to the choice screen", () => {
  vi.stubGlobal("fetch", vi.fn());
  const { view, onOrder } = mount();
  fireEvent.click(screen.getByRole("button", { name: "wipe" }));
  expect(screen.getByTestId("control-window").textContent).toMatch(/тело пересоздаётся/);
  view.rerender(<MantineProvider>
    <ControlWindow opened row={{ ...ROW, name: "pu-mop-2" }} session={ADMIN}
      onClose={vi.fn()} onOrder={vi.fn()} />
  </MantineProvider>);
  const win = screen.getByTestId("control-window");
  expect(win.textContent).toContain("pu-mop-2");
  expect(screen.queryByRole("button", { name: /^подтвердить$/i })).toBeNull();
  expect(screen.getByRole("button", { name: "обновление" })).toBeInTheDocument();
  expect(onOrder).not.toHaveBeenCalled();
});
