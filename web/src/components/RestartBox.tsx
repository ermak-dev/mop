// Restart цели (#465/#513): клик шлёт POST сразу, обратим -- моды
// подтверждения нет (подтверждение осталось у необратимых: разрушительные
// #473 и update в fresh-режимах). Если рестарт невозможен по статусу цели
// (#508) -- аллокации нет (bucket down) или задача падает (kind failing)
// -- вход дизейблится с причиной в подписи: признак уже посчитан сервером
// в строке, отказ после клика был бы вторым ответом на вопрос строки.
// #516: точка входа -- пункт окна правки (icon=false + сигнал-токен);
// иконка в строке ушла, боксы остались смонтированными в строке --
// вердикт операции переживает окно. Слова вердикта -- модель #476:
// running -- «идёт», unknown -- «неизвестно» (НЕ failed: эффекта могли и
// не быть), автоповтора нет -- повтор это новый клик. Никакого force:
// params пусты, схема -- #461. Результат операции в памяти страницы:
// перезагрузка начинает заново -- id хранить негде (#463 запретил
// storage), это названо в отчёте #465.
import { useState } from "react";
import { IconRefresh } from "@tabler/icons-react";
import { IconButton } from "./IconButton";
import { OperationResult } from "./OperationResult";
import { useSignal, type Signal } from "./ControlWindow";
import { Group } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";

/** Слово вердикта для человека: #476, своего перечня здесь нет. */
export function opWord(status: string): string {
  return {
    idle: "", submitted: "отправляется", accepted: "принято",
    queued: "ждёт очереди", waiting: "ждёт цель", running: "идёт",
    succeeded: "перезапущен",
    refused: "отказано", failed: "не удался", partial: "частично: эффект без наблюдения",
    unknown: "исход неизвестен",
  }[status] ?? status;
}

/** Почему рестарт невозможен по статусу цели (#508): null -- возможен.
 * Признаки -- из строки: bucket down (без бегущей аллокации спрашивать
 * некого), kind failing (задача падает, поднимать нечего). */
export function restartReason(row: Puppet): string | null {
  if (row.bucket === "down") return "рестарт недоступен: не запущен";
  if (row.kind === "failing") return "рестарт недоступен: задача падает";
  return null;
}

export function RestartBox({ session, row, icon = true, signal = null }: {
  session: Session;
  /** Строка ростера: цель рестарта и её признаки. */
  row: Puppet;
  /** Кожа иконки в строке (#516): пункт окна шлёт сигнал без иконки. */
  icon?: boolean;
  /** Сигнал-токен пункта окна (#516): срабатывание по изменению. */
  signal?: Signal;
}) {
  const [result, setResult] = useState(false);
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  const blocked = restartReason(row);
  // #508: причина дизейбла едет в подпись -- Tooltip и aria-label
  // пишутся из одного слова, человек и проверка видят одно.
  const label = blocked ?? "рестарт";
  // Вход (#516): пункт окна или клик иконки -- один и тот же ход
  // (семантика #513 дословно): живой или завершённый ход показывает
  // результат, новый POST -- только из «новая команда» или свободного
  // бокса.
  const enter = () => { if (op.status !== "idle") setResult(true);
                        else { setResult(true); void submit(); } };
  useSignal(signal, enter);
  return (
    <Group gap="sm">
      {icon && (
        <IconButton label={label} variant="subtle" color="orange" size="lg"
                disabled={!!blocked}
                onClick={() => { setResult(true); void submit(); }}>
          <IconRefresh size={18} />
        </IconButton>
      )}
      <OperationResult opened={result} onClose={() => setResult(false)}
        onAgain={() => { setResult(false); void submit(); }} title="рестарт"
        target={row.name} op={op} word={opWord(op.status)} testId="restart-verdict" />
    </Group>
  );
}
