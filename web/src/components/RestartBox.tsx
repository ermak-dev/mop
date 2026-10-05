// Restart из строки папета (#465): иконка, вердикт операции. #513:
// рестарт обратим -- клик шлёт POST сразу, мода точного подтверждения
// ушла (подтверждение осталось у необратимых: разрушительные #473 и
// update в fresh-режимах). Слова вердикта -- модель #476: running --
// «идёт», unknown -- «неизвестно» (НЕ failed: эффекта могли и не быть),
// автоповтора нет -- повтор это новый клик. Никакого force: params
// пусты, схема -- #461. Результат операции в памяти страницы:
// перезагрузка начинает заново -- id хранить негде (#463 запретил
// storage), это названо в отчёте #465.
import { useState } from "react";
import { IconRefresh } from "@tabler/icons-react";
import { IconButton } from "./IconButton";
import { OperationResult } from "./OperationResult";
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

export function RestartBox({ session, row }: {
  session: Session;
  /** Строка ростера: цель рестарта. */
  row: Puppet;
}) {
  const [result, setResult] = useState(false);
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  return (
    <Group gap="sm">
      <IconButton label="рестарт" variant="subtle" color="orange" size="lg"
              onClick={() => { if (op.status !== "idle") setResult(true);
                               else { setResult(true); void submit(); } }}>
        <IconRefresh size={18} />
      </IconButton>
      <OperationResult opened={result} onClose={() => setResult(false)}
        onAgain={() => { setResult(false); void submit(); }} title="рестарт"
        target={row.name} op={op} word={opWord(op.status)} testId="restart-verdict" />
    </Group>
  );
}
