// Restart из строки папета: иконка, точное подтверждение и
// вердикт операции. Подтверждение показывает факты строки (имя, узел,
// state-строка) -- подтверждается ровно то, что на экране; слова вердикта
// -- модель #476: running -- «идёт», unknown -- «неизвестно» (НЕ failed:
// эффекта могли и не быть), автоповтора нет -- повтор это новый клик и
// новое подтверждение. Никакого force: params пусты, схема -- #461.
// Результат операции в памяти страницы: перезагрузка начинает заново --
// id хранить негде (#463 запретил storage), это названо в отчёте #465.
import { useState } from "react";
import { IconRefresh } from "@tabler/icons-react";
import { IconButton } from "./IconButton";
import { OperationResult } from "./OperationResult";
import { Button, Code, Group, Modal, Stack, Text } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";

/** Слово вердикта для человека: #476, своего перечня здесь нет. */
export function opWord(status: string): string {
  return {
    idle: "", submitted: "отправляется", accepted: "принято",
    queued: "ждёт очереди", running: "идёт", succeeded: "перезапущен",
    refused: "отказано", failed: "не удался", partial: "частично: эффект без наблюдения",
    unknown: "исход неизвестен",
  }[status] ?? status;
}

export function RestartBox({ session, row }: {
  session: Session;
  /** Строка ростера: подтверждение показывает её факты. */
  row: Puppet;
}) {
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState(false);
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  return (
    <Group gap="sm">
      <IconButton label="рестарт" variant="subtle" color="orange" size="lg"
              onClick={() => { if (op.status !== "idle") setResult(true); else setOpen(true); }}>
        <IconRefresh size={18} />
      </IconButton>
      <OperationResult opened={result} onClose={() => setResult(false)}
        onAgain={() => { setResult(false); setOpen(true); }} title="рестарт"
        target={row.name} op={op} word={opWord(op.status)} testId="restart-verdict" />
      {/* модал рендерится только открытым: закрытого контента в DOM нет
          (Mantine иначе держит его под спрятанным переходом) */}
      {open && (
      <Modal opened onClose={() => setOpen(false)} size="md"
             title="подтвердите рестарт" data-testid="restart-confirm"
             closeButtonProps={{ "aria-label": "закрыть" }}>
        <Stack gap="sm">
          {/* точная цель: что подтверждается -- то и на экране */}
          <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
          <Text size="sm">узел: {row.node}</Text>
          <Text size="sm">состояние: <Code>{row.state}</Code></Text>
          <Text size="sm" c="dimmed">
            панель перерегистрирует job; сила (force) не применяется
          </Text>
          <Group justify="flex-end" gap="sm">
            <Button variant="light" color="gray" onClick={() => setOpen(false)}>
              отмена
            </Button>
            <Button color="orange" onClick={() => { setOpen(false); setResult(true); void submit(); }}>
              подтвердить
            </Button>
          </Group>
        </Stack>
      </Modal>
      )}
    </Group>
  );
}
