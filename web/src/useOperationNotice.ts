import { useEffect, useRef } from "react";
import { notifications } from "@mantine/notifications";
import { TERMINAL, type AdminOpState } from "./useAdminOp";

interface Options {
  scope: string;
  title: string;
  word: string;
  enabled: boolean;
  notifySuccess?: boolean;
  onSettled?: () => void;
}

/** Каждый терминальный снимок -- исход одной попытки, даже без server id.
 * Смена владельца или цели не должна показывать прежний исход новому читателю. */
export function useOperationNotice(op: AdminOpState, {
  scope, title, word, enabled, notifySuccess = true, onSettled,
}: Options) {
  const source = useRef(scope);
  const shown = useRef<AdminOpState | null>(null);
  if (source.current !== scope) {
    source.current = scope;
    shown.current = op;
  }
  useEffect(() => {
    if (!enabled || !TERMINAL.has(op.status) || shown.current === op) return;
    shown.current = op;
    if (notifySuccess || op.status !== "succeeded") {
      notifications.show({
        title,
        color: op.status === "succeeded" ? "teal"
          : op.status === "unknown" || op.status === "partial" ? "yellow" : "red",
        message: `${word}${op.error ? `: ${op.error}` : ""}${op.lost ? " (первый ответ потерян)" : ""}`,
        autoClose: 8000,
      });
    }
    onSettled?.();
  }, [op, scope, title, word, enabled, notifySuccess, onSettled]);
}
