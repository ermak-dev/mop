// Обновление (#470 -> #522): три явных режима fresh -- keep/replace/
// remove, никаких умолчаний. Радио режимов и финальный экран
// «что произойдёт» собирает визард ControlWindow на первом шаге
// (inline под пунктом) -- приказ приезжает токеном
// {at, act: "update", params: {fresh[, origin]}} с кнопки «Завершить».
// Движок без кожи и без собственной моды (#522): шлёт POST и поднимает
// стейт операции наверх (onState {status, error, lost}) -- экран хода
// рисует визард. Ход операции -- общий useAdminOp; результат в памяти
// страницы, перезагрузка начинает заново.
// Повторный вход (#487): визард видит поднятый стейт и открывает экран
// хода; повтор -- «новая команда» на экране хода возвращает к выбору.
import { useEffect } from "react";
import { useOrder, useSignal, type Signal, type EngineState } from "./ControlWindow";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";

/** Последствия режима: что гибнет, что живёт -- слова #473. Словарь
 * живёт у движка, визард его показывает: источник один. */
export const MODES: Record<string, { label: string; lines: string[] }> = {
  keep: {
    label: "продолжить как было",
    lines: [
      "Разговор продолжится: контекст сессии остаётся.",
      "Несохранённая работа в клоне не трогается: ничего не гибнет.",
    ],
  },
  replace: {
    label: "свежий клон, тот же origin",
    lines: [
      "Клон будет пересобран начисто: несохранённая работа погибнет.",
      "Разговор погибнет: сессия начнётся с нуля.",
    ],
  },
  remove: {
    label: "другой origin (пересадка)",
    lines: [
      "Клон будет пересобран от нового origin: несохранённая работа погибнет.",
      "Разговор погибнет: сессия начнётся с нуля.",
      "Папет продолжит жить под тем же именем.",
    ],
  },
};

export function UpdateBox({ session, row, order = null, forget = null, onState }: {
  session: Session;
  /** Строка ростера: цель обновления. */
  row: Puppet;
  /** Приказ-токен визарда (#522): «Завершить» с финального экрана. */
  order?: Signal;
  /** Забыть показанный вердикт (#522): «новая команда» из футера хода. */
  forget?: Signal;
  /** Стейт хода вверх (#522): визард рисует экран хода. */
  onState?: (op: EngineState) => void;
}) {
  const { op, submit, forget: settle } = useAdminOp(session, session.role === "admin" ? row.name : null);
  useEffect(() => { onState?.(op); }, [op, onState]);
  // Вход (#522): приказ здесь -- всегда новая команда с финального
  // экрана визарда, терминальный ход не глотается; дедуп живого хода --
  // движковый (useAdminOp). Замыкание свежего рендера держит сам
  // useOrder (latest-ref): свой useRef здесь ловил бы первый рендер и
  // слал бы повторный POST мимо вердикта.
  useOrder(order, "update", (params) => { void submit("update", params); });
  // «Новая команда» (#522): показанный вердикт стирается, иначе pick того
  // же пункта навсегда diverted бы на старый терминал.
  useSignal(forget, settle);
  return null;
}
