// BUG: #664: управление целью повторно открывает экран вместо выбора строки.
// HYPOTHESIS: PuppetActions всё ещё владеет экраном и вкладывает PuppetCard.
// SOLUTION: #664: экран и его стейт убраны, визард и движки остаются на месте.
// RESULT: отдельная правка открывает визард без второго просмотрщика.
// STATUS: FIXED — see #664
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PuppetActions } from "./PuppetActions";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";

const row: Puppet = {
  name: "pu-mop-1", node: "node", alloc_status: "running", state: "free (master)",
  kind: "free", bucket: "free", detail: null, branch: "master", owner: "",
  llm: "model", origin: "git@example.test:g/mop.git", disk_kb: null,
};
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("#664 target actions offer edit without a nested screen control", () => {
  vi.stubGlobal("fetch", vi.fn());
  renderUi(<PuppetActions row={row} session={{ login: "admin", role: "admin" }} />);
  const actions = screen.getByRole("group", { name: `действия ${row.name}` });
  expect(within(actions).queryByRole("button", { name: "Экран" })).toBeNull();
  expect(within(actions).getAllByRole("button").map(button => button.getAttribute("aria-label")))
    .toEqual(["Правка"]);
  fireEvent.click(within(actions).getByRole("button", { name: "Правка" }));
  const win = screen.getByTestId("control-window");
  expect(within(win).getByRole("button", { name: "Рестарт" })).toBeInTheDocument();
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(screen.queryByRole("complementary", { name: `экран ${row.name}` })).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
});
