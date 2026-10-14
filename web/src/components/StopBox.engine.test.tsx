// HYPOTHESIS (#608): приказ остановки должен дойти до общего HTTP движка один раз.
import { cleanup } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { StopBox } from "./StopBox";
import { MantineProvider } from "@mantine/core";
import { renderUi } from "../test-utils";
import type { Puppet } from "../types";

const hooks = vi.hoisted(() => ({ submit: vi.fn(), forget: vi.fn(), op: { status: "idle" } }));
vi.mock("../useAdminOp", async (original) => ({
  ...await original<typeof import("../useAdminOp")>(),
  useAdminOp: () => hooks,
}));
const row: Puppet = { name: "pu-mop-1", node: "-", alloc_status: "pending",
  state: "starting", kind: "starting", owner: "-", llm: "-",
  origin: "git@example.test:mop.git", disk_kb: null };
afterEach(() => { cleanup(); vi.clearAllMocks(); });

test("#608 the confirmed stop order reaches the HTTP engine once", () => {
  const props = { session: { login: "anton", role: "admin" as const }, row };
  const view = renderUi(<StopBox {...props} />);
  expect(hooks.submit).not.toHaveBeenCalled();
  const order = { at: 1, act: "stop", params: {} };
  view.rerender(<MantineProvider><StopBox {...props} order={order} /></MantineProvider>);
  expect(hooks.submit).toHaveBeenCalledExactlyOnceWith("stop", {});
  view.rerender(<MantineProvider><StopBox {...props} order={order} /></MantineProvider>);
  expect(hooks.submit).toHaveBeenCalledTimes(1);
  view.rerender(<MantineProvider><StopBox {...props}
    order={{ at: 2, act: "restart", params: {} }} /></MantineProvider>);
  expect(hooks.submit).toHaveBeenCalledTimes(1);
});
