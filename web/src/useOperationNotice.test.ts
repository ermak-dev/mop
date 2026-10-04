// #492: балуны общие для форм; один исход попытки -- одно уведомление,
// отсутствие id не скрывает отказ, смена цели не переносит старую ошибку.
// STATUS: FIXED — see #492
import { renderHook } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { useOperationNotice } from "./useOperationNotice";
import type { AdminOpState } from "./useAdminOp";

const notice = vi.hoisted(() => ({ show: vi.fn() }));
vi.mock("@mantine/notifications", () => ({ notifications: { show: notice.show } }));
const failed = (): AdminOpState => ({ id: null, status: "refused", error: "safe refusal", lost: false });

beforeEach(() => notice.show.mockClear());

test("#492 rerenders do not duplicate a terminal notice, another id-less attempt does", () => {
  const op = failed();
  const settled = vi.fn();
  const view = renderHook(({ state }) => useOperationNotice(state, {
    scope: "admin/pu-1", title: "target", word: "отказано", enabled: true, onSettled: settled,
  }), { initialProps: { state: op } });
  expect(notice.show).toHaveBeenCalledTimes(1);
  view.rerender({ state: op });
  expect(notice.show).toHaveBeenCalledTimes(1);
  expect(settled).toHaveBeenCalledTimes(1);
  view.rerender({ state: failed() });
  expect(notice.show).toHaveBeenCalledTimes(2);
  expect(settled).toHaveBeenCalledTimes(2);
});

test("#492 a different source cannot replay the former terminal result", () => {
  const op = failed();
  const view = renderHook(({ scope, state, enabled }) => useOperationNotice(state, {
    scope, title: scope, word: "отказано", enabled,
  }), { initialProps: { scope: "admin/pu-1", state: op, enabled: true } });
  expect(notice.show).toHaveBeenCalledTimes(1);
  view.rerender({ scope: "guest/pu-1", state: op, enabled: false });
  view.rerender({ scope: "admin/pu-2", state: op, enabled: true });
  expect(notice.show).toHaveBeenCalledTimes(1);
  view.rerender({ scope: "admin/pu-2", state: failed(), enabled: true });
  expect(notice.show).toHaveBeenCalledTimes(2);
  expect(notice.show.mock.calls[1][0].title).toBe("admin/pu-2");
});

test("#492 partial results warn instead of claiming failure or success", () => {
  renderHook(() => useOperationNotice({ id: "op-1", status: "partial", error: null, lost: false }, {
    scope: "admin/pu-1", title: "target", word: "частично", enabled: true,
  }));
  expect(notice.show).toHaveBeenCalledWith(expect.objectContaining({ color: "yellow", message: "частично" }));
});
