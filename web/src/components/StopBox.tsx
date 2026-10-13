// Остановка задания (#608): без удаления клона и без автоматического повтора.
import { useEffect } from "react";
import { useOrder, useSignal, type Signal, type EngineState } from "./ControlWindow";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";

export const STOP_EFFECTS = [
  "Инициализация и текущая сессия прекращаются.",
  "Контейнер и рабочая копия сохраняются.",
  "Повторные запуски прекращаются до явного обновления папета.",
];

export function StopBox({ session, row, order = null, forget = null, onState }: {
  session: Session;
  row: Puppet;
  order?: Signal;
  forget?: Signal;
  onState?: (op: EngineState) => void;
}) {
  const { op, submit, forget: settle } = useAdminOp(session,
    session.role === "admin" ? row.name : null);
  useEffect(() => { onState?.(op); }, [op, onState]);
  useOrder(order, "stop", () => { void submit("stop", {}); });
  useSignal(forget, settle);
  return null;
}
