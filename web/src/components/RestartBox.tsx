// Рестарт цели (#465/#513 -> #522): движок без кожи и без собственной
// моды. Визард ControlWindow шлёт приказ-токен {at, act: "restart",
// params: {}} с финального экрана (#522: единое правило подтверждения
// -- финал у всех, immediate-POST ушёл), движок шлёт POST и поднимает
// стейт операции наверх (onState {status, error, lost}) -- экран хода
// рисует визард. Если рестарт невозможен по статусу цели (#508) --
// пункт дизейблится с причиной в подписи; признак уже посчитан
// сервером в строке, отказ после клика был бы вторым ответом на
// вопрос строки. Слова вердикта -- модель #476: running -- «идёт»,
// unknown -- «неизвестно» (НЕ failed: эффекта могли и не быть),
// автоповтора нет -- повтор это новая команда. Никакого force:
// params пусты, схема -- #461. Результат операции в памяти страницы:
// перезагрузка начинает заново (#463 запретил storage).
import { useEffect } from "react";
import { useOrder, useSignal, type Signal, type EngineState } from "./ControlWindow";
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

/** Последствия рестарта (#521, словарь замен #527): тем же голосом,
 * что PREVIEW/MODES -- простые утверждения, что гибнет и что живёт.
 * Рестарт поднимает задачу заново на той же рабочей копии: контекст
 * диалога теряется (это и лечение от залипания -- липкое продолжение
 * возвращало бы болезнь, spec.py), несохранённая работа не трогается. */
export const RESTART_EFFECTS = [
  "Рабочая копия не трогается: несохранённая работа живёт.",
  "Контекст диалога с Claude теряется: сессия поднимается заново.",
];

/** Почему рестарт невозможен по статусу цели (#508): null -- возможен.
 * Признаки -- из строки: bucket down (без бегущей аллокации спрашивать
 * некого), kind failing (задача падает, поднимать нечего). */
export function restartReason(row: Puppet): string | null {
  if (row.bucket === "down") return "рестарт недоступен: не запущен";
  if (row.kind === "failing") return "рестарт недоступен: задача падает";
  return null;
}

export function RestartBox({ session, row, order = null, forget = null, onState }: {
  session: Session;
  /** Строка ростера: цель рестарта. */
  row: Puppet;
  /** Приказ-токен визарда (#522): срабатывание по изменению. */
  order?: Signal;
  /** Забыть показанный вердикт (#522): «новая команда» из футера хода. */
  forget?: Signal;
  /** Стейт хода вверх (#522): визард рисует экран хода -- статус,
   * причина и потерянный ответ едут одним объектом. */
  onState?: (op: EngineState) => void;
}) {
  const { op, submit, forget: settle } = useAdminOp(session, session.role === "admin" ? row.name : null);
  useEffect(() => { onState?.(op); }, [op, onState]);
  // Вход (#522): приказ -- всегда новая команда с финального экрана
  // визарда; повторный вход при живом ходе визард пускает экраном хода
  // (engineState), сюда ордер не доходит. Дедуп живого хода --
  // движковый (useAdminOp). Замыкание свежего рендера держит сам
  // useOrder (latest-ref).
  useOrder(order, "restart", () => { void submit(); });
  // «Новая команда» (#522): показанный вердикт стирается, иначе pick того
  // же пункта навсегда diverted бы на старый терминал.
  useSignal(forget, settle);
  return null;
}
