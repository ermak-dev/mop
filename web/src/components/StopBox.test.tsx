// HYPOTHESIS (#608): визард не умеет остановить задание в init/backoff.
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { ControlWindow } from "./ControlWindow";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";

const row: Puppet = {
  name: "pu-mop-1", node: "hyper", alloc_status: "pending",
  state: "starting", kind: "starting", bucket: "down", detail: null,
  branch: "master", owner: "-", llm: "-",
  origin: "git@example.test:mop.git", disk_kb: null,
};
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("#608 stopping initialization requires confirmation and preserves the clone", () => {
  vi.stubGlobal("fetch", vi.fn());
  const onOrder = vi.fn();
  renderUi(<ControlWindow opened row={row} session={{ login: "anton", role: "admin" }}
    onOrder={onOrder} onClose={vi.fn()} />);
  const win = screen.getByTestId("control-window");
  fireEvent.click(within(win).getByRole("button", { name: "Остановить" }));
  expect(onOrder).not.toHaveBeenCalled();
  expect(within(win).getByText("Контейнер и рабочая копия сохраняются.")).toBeInTheDocument();
  expect(within(win).getByText("Повторные запуски прекращаются до явного обновления папета.")).toBeInTheDocument();
  fireEvent.click(within(win).getByRole("button", { name: "Завершить" }));
  expect(onOrder).toHaveBeenCalledExactlyOnceWith({ act: "stop", params: {} });
});

test("#608 completed stop is not presented as a restart", () => {
  vi.stubGlobal("fetch", vi.fn());
  const props = { opened: true, row, session: { login: "anton", role: "admin" as const },
    onOrder: vi.fn(), onClose: vi.fn() };
  const view = renderUi(<ControlWindow {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Остановить" }));
  fireEvent.click(screen.getByRole("button", { name: "Завершить" }));
  view.rerender(<MantineProvider><ControlWindow {...props}
    engineState={{ stop: { status: "succeeded" } }} /></MantineProvider>);
  expect(screen.getByTestId("progress-word")).toHaveTextContent("Остановлен");
  expect(screen.queryByText("Перезапущен")).toBeNull();
});
