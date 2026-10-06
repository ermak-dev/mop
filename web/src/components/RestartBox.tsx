// Рестарт цели (#465/#513 -> #520): движок без кожи -- визард ControlWindow
// шлёт приказ-токен {at, act: "restart", params: {}} с первого экрана,
// клик по пункту шлёт POST сразу (обратим, моды подтверждения нет,
// #513). Если рестарт невозможен по статусу цели (#508) -- пункт
// дизейблится с причиной в подписи; признак уже посчитан сервером в
// строке, отказ после клика был бы вторым ответом на вопрос строки.
// Слова вердикта -- модель #476: running -- «идёт», unknown --
// «неизвестно» (НЕ failed: эффекта могли и не быть), автоповтора нет --
// повтор это новая команда. Никакого force: params пусты, схема -- #461.
// Результат операции в памяти страницы: перезагрузка начинает заново
// (#463 запретил storage), это названо в отчёте #465.
import { useEffect, useState } from "react";
import { OperationResult } from "./OperationResult";
import { useOrder, useSignal, type Signal } from "./ControlWindow";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";

/** Слово вердикта для человека: #476, своего перечня здесь нет. */
export function opWord(status: string): string {
  return {
    idle: "", submitted: "Отправляется", accepted: "Принято",
    queued: "Ждёт очереди", waiting: "Ждёт цель", running: "Идёт",
    succeeded: "Перезапущен",
    refused: "Отказано", failed: "Не удался", partial: "Частично: эффект без наблюдения",
    unknown: "Исход неизвестен",
  }[status] ?? status;
}

/** Последствия рестарта (#521): тем же голосом, что PREVIEW/MODES --
 * простые утверждения, что гибнет и что живёт. Рестарт поднимает
 * задачу заново на том же клоне: разговор теряется (это и лечение от
 * залипания -- липкое продолжение возвращало бы болезнь, spec.py),
 * несохранённая работа в клоне не трогается. */
export const RESTART_EFFECTS = [
  "Клон не трогается: несохранённая работа живёт.",
  "Разговор теряется: сессия поднимается заново.",
];

/** Почему рестарт невозможен по статусу цели (#508): null -- возможен.
 * Признаки -- из строки: bucket down (без бегущей аллокации спрашивать
 * некого), kind failing (задача падает, поднимать нечего). */
export function restartReason(row: Puppet): string | null {
  if (row.bucket === "down") return "рестарт недоступен: не запущен";
  if (row.kind === "failing") return "рестарт недоступен: задача падает";
  return null;
}

export function RestartBox({ session, row, order = null, reveal = null, onState }: {
  session: Session;
  /** Строка ростера: цель рестарта. */
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
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  useEffect(() => { onState?.(op.status); }, [op.status, onState]);
  useSignal(reveal, () => setResult(true));
  // Вход (#520): приказ визарда; повторный вход при живом или терминальном
  // ходе визард divert-ит на вердикт (engineState, #487), приказ здесь --
  // новая команда и постится; дедуп живого хода -- движковый (useAdminOp).
  // Замыкание свежего рендера держит сам useOrder (latest-ref).
  useOrder(order, "restart", () => {
    setResult(true); void submit();
  });
  if (session.role !== "admin") return null;
  return (
    <OperationResult opened={result} onClose={() => setResult(false)}
      onAgain={() => { setResult(false); void submit(); }} title="рестарт"
      target={row.name} op={op} word={opWord(op.status)} testId="restart-verdict" />
  );
}
