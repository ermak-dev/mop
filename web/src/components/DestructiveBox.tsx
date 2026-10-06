// Разрушительные действия (#473 -> #520): recycle/delete/wipe -- три
// отдельных действия, никаких обобщённых «force». #520: единый экран
// последствий (что гибнет + факты точной цели + одна «подтвердить»)
// собирает визард ControlWindow; движок шлёт POST по приказ-токену
// {at, act, params} и показывает вердикт OperationResult'ом (#465,
// единый механизм с рестартом/обновлением). Stale-защита движковая:
// смена источника (роль, цель) стирает операцию в тот же рендер
// (useAdminOp), молча ретраить по имени нельзя. Частичный исход --
// «частично», recovery не обещается. Никакого force: params пусты,
// схема -- #461.
import { useEffect, useState } from "react";
import { OperationResult } from "./OperationResult";
import { useOrder, useSignal, type Signal } from "./ControlWindow";
import { opWord } from "./RestartBox";
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

export function DestructiveBox({ session, row, order = null, reveal = null, onState }: {
  session: Session;
  /** Строка ростера: цель действия, её факты подтверждал визард. */
  row: Puppet;
  /** Приказ-токен визарда (#520): срабатывание по изменению. */
  order?: Signal;
  /** Приказ «показать сохранённый вердикт» (#487): от визарда. */
  reveal?: Signal;
  /** Состояние хода вверх (#487): как у обновления -- визард при
   * повторном входе показывает вердикт, не молчит. */
  onState?: (status: string) => void;
}) {
  const [result, setResult] = useState(false);
  const [title, setTitle] = useState("операция");
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  useEffect(() => { onState?.(op.status); }, [op.status, onState]);
  useSignal(reveal, () => setResult(true));
  // Вход (#520): приказ визарда; живой или завершённый ход показывает
  // результат, второго POST нет (движковый дедуп), повтор -- новый
  // приказ через визард. Замыкание свежего рендера держит сам useOrder
  // (latest-ref): свой useRef здесь ловил бы первый рендер.
  const enter = (act: string, params: Record<string, unknown>) => {
    setTitle(act);
    setResult(true);
    void submit(act, params);
  };
  useOrder(order, "recycle", (p) => enter("recycle", p));
  useOrder(order, "delete", (p) => enter("delete", p));
  useOrder(order, "wipe", (p) => enter("wipe", p));
  if (session.role !== "admin") return null;
  return (
    <OperationResult opened={result} onClose={() => setResult(false)}
      onAgain={() => { setResult(false); }} title={title}
      target={row.name} op={op} word={opWord(op.status)} testId="destructive-verdict" />
  );
}
