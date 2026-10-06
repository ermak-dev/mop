// Разрушительные действия (#473 -> #522): recycle/delete/wipe -- три
// отдельных действия, никаких обобщённых «force». Финальный экран
// последствий (что гибнет + факты точной цели + «Завершить» =
// согласие) собирает визард ControlWindow; движок без кожи и без
// собственной моды (#522): шлёт POST по приказ-токену
// {at, act, params} и поднимает стейт операции наверх (onState
// {status, error, lost}) -- экран хода рисует визард. Stale-защита
// движковая: смена источника (роль, цель) стирает операцию в тот же
// рендер (useAdminOp), молча ретраить по имени нельзя. Частичный исход
// -- «частично», recovery не обещается. Никакого force: params пусты,
// схема -- #461.
import { useEffect } from "react";
import { useOrder, useSignal, type Signal, type EngineState } from "./ControlWindow";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";

/** Последствия действия (#456/#471): что гибнет, что живёт. Словарь
 * живёт у движка, визард его показывает: источник один. */
export const PREVIEW: Record<string, { title: string; lines: string[] }> = {
  recycle: { title: "последствия recycle", lines: [
    "Клон будет возвращён к origin/ветке: несохранённая работа погибнет.",
    "Каталог цели будет вычищен целиком: первая сборка после пересоздания медленная.",
    "Джоб и тело останутся жить: папет поднимется заново на чистом клоне.",
  ]},
  delete: { title: "последствия delete", lines: [
    "Клон и джоб удаляются, папета больше нет.",
    "Тело — контейнер: уйдут память и диск вместе с ним.",
  ]},
  wipe: { title: "последствия wipe", lines: [
    "Клон будет сброшен к HEAD, каталог цели вычищен.",
    "Тело пересоздаётся, разговор теряется.",
    "Живая сессия — отказ: сброс — для остановленного, полный цикл — пересоздание.",
  ]},
};

export function DestructiveBox({ session, row, order = null, forget = null, onState }: {
  session: Session;
  /** Строка ростера: цель действия, её факты подтверждал визард. */
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
  // Вход (#522): приказ -- всегда новая команда; живой или завершённый
  // ход визард показывает экраном хода по поднятому стейту, второго
  // POST нет (движковый дедуп). Замыкание свежего рендера держит сам
  // useOrder (latest-ref): свой useRef здесь ловил бы первый рендер.
  useOrder(order, "recycle", (p) => { void submit("recycle", p); });
  useOrder(order, "delete", (p) => { void submit("delete", p); });
  useOrder(order, "wipe", (p) => { void submit("wipe", p); });
  // «Новая команда» (#522): показанный вердикт стирается, иначе pick того
  // же пункта навсегда diverted бы на старый терминал.
  useSignal(forget, settle);
  return null;
}
