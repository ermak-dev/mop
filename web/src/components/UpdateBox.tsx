// Обновление (#470 -> #520): три явных режима fresh -- keep/replace/
// remove, никаких умолчаний. #520: шаг параметров (радио трёх режимов
// #470 + поле origin для пересадки) и единый финальный экран
// (последствия + факты цели) собирает визард ControlWindow; движок
// шлёт POST по приказ-токену {at, act: "update", params: {fresh[,
// origin]}} и показывает вердикт OperationResult'ом. keep -- обратим
// (#513): визард шлёт приказ сразу с шага параметров; replace/remove
// необратимы -- у них подтверждение точной цели на финальном экране
// визарда, как у разрушительных (#473). Ход операции -- общий
// useAdminOp; результат в памяти страницы, перезагрузка начинает
// заново.
// Повторный вход (#487/#513): визард спрашивает у движка состояние
// (onState) и приказывает показать непоказанный ход (reveal) -- формы
// не открывает, второго POST нет; повтор -- «новая команда» в вердикте:
// она возвращает визард на шаг обновления (onReopen), и подтверждённая
// новая команда постится: терминальный ход не глотается.
import { useEffect, useState } from "react";
import { OperationResult } from "./OperationResult";
import { useOrder, useSignal, type Signal } from "./ControlWindow";
import { opWord } from "./RestartBox";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";

/** Последствия режима: что гибнет, что живёт -- слова #473. Словарь
 * живёт у движка, визард его показывает: источник один. */
export const MODES: Record<string, { label: string; lines: string[] }> = {
  keep: {
    label: "продолжить как было",
    lines: [
      "разговор продолжится: контекст сессии остаётся",
      "несохранённая работа в клоне не трогается: ничего не гибнет",
    ],
  },
  replace: {
    label: "свежий клон, тот же origin",
    lines: [
      "клон будет пересобран начисто: несохранённая работа погибнет",
      "разговор погибнет: сессия начнётся с нуля",
    ],
  },
  remove: {
    label: "другой origin (пересадка)",
    lines: [
      "клон будет пересобран от нового origin: несохранённая работа погибнет",
      "разговор погибнет: сессия начнётся с нуля",
      "папет продолжит жить под тем же именем",
    ],
  },
};

export function UpdateBox({ session, row, order = null, reveal = null, onReopen, onState }: {
  session: Session;
  /** Строка ростера: цель обновления. */
  row: Puppet;
  /** Приказ-токен визарда (#520): срабатывание по изменению. */
  order?: Signal;
  /** Приказ «показать сохранённый вердикт» (#487): визард шлёт его при
   * повторном входе, когда видит от движка непустой статус; ход уже
   * шёл, второго POST нет. */
  reveal?: Signal;
  /** «новая команда» открывает визард заново на шаге обновления:
   * повтор -- новый выбор режима, не молчаливый повтор POST. */
  onReopen?: () => void;
  /** Состояние хода вверх (#487): визард при повторном входе узнаёт,
   * есть ли непоказанный ход, и приказывает показать его вердикт
   * (reveal), а не открывать форму поверх. */
  onState?: (status: string) => void;
}) {
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  const [result, setResult] = useState(false);
  useEffect(() => { onState?.(op.status); }, [op.status, onState]);
  // Вход (#520): приказ визарда; повторный вход при живом или терминальном
  // ходе визард не пускает сюда вовсе -- divert на вердикт (engineState,
  // #487), так что приказ здесь всегда новая команда и постится; дедуп
  // живого хода -- движковый (useAdminOp). Замыкание свежего рендера
  // держит сам useOrder (latest-ref): свой useRef здесь ловил бы первый
  // рендер и слал бы повторный POST мимо вердикта.
  useOrder(order, "update", (params) => {
    setResult(true);
    void submit("update", params);
  });
  useSignal(reveal, () => setResult(true));
  if (session.role !== "admin") return null;
  return (
    <OperationResult opened={result} onClose={() => setResult(false)}
      onAgain={() => { setResult(false); onReopen?.(); }}
      title="обновление папета" target={row.name}
      op={op} word={op.status === "succeeded" ? "обновлено" : opWord(op.status)}
      testId="update-verdict" />
  );
}
