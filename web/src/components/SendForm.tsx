// Форма адресного send в отдельном диалоге папета,
// на том же общем ходе операции (useAdminOp): POST
// /api/admin/ops {action: "send", params: {message}} -> id -> вердикт.
// Политика первого выпуска #461: адресная доставка, никакого wait/notify
// в UI. Слова вердикта -- модель #476, как у RestartBox (opWord):
// succeeded -- «доставлено» только для send, остальное -- общий словарь.
// Payload живёт в поле на время полёта; в storage и URL его нет --
// запрет storage #463 держит и здесь.
import { useState } from "react";
import { Button, Group, Text, Textarea } from "@mantine/core";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";
import { opWord } from "./RestartBox";

/** Слово исхода send: свой глагол у «succeeded», остальное -- общий словарь. */
export const sendWord = (status: string): string =>
  status === "succeeded" ? "доставлено" : opWord(status);

const TERMINAL = new Set(["succeeded", "refused", "failed", "partial", "unknown"]);

export function SendForm({ session, target }: { session: Session; target: string }) {
  // третий аргумент хука -- pollMs (#473): действие уезжает в submit, не сюда
  const { op, submit } = useAdminOp(session, session.role === "admin" ? target : null);
  const [message, setMessage] = useState("");
  const live = op.status !== "idle" && !TERMINAL.has(op.status);

  const send = () => {
    if (!message.trim() || live) return;    // пустое не постится, живой ход один
    void submit("send", { message: message.trim() });
    setMessage("");                         // текст уехал: в поле его больше нет
  };

  return (
    <form data-testid="send-form" onSubmit={(e) => { e.preventDefault(); send(); }}>
      <Textarea
        label="Сообщение"
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        minRows={3}
        data-testid="send-text"
      />
      <Group justify="flex-end" mt="xs">
        <Button size="xs" onClick={send} loading={op.status === "submitted"}
                data-testid="send-send">Отправить</Button>
      </Group>
      {op.status !== "idle" && (
        <Text size="sm" c={op.status === "succeeded" ? "teal"
                            : op.status === "unknown" ? "yellow" : "red"}
              data-testid="send-verdict">
          {sendWord(op.status)}
          {op.lost ? " (первый ответ потерян)" : ""}
        </Text>
      )}
      {op.error ? <Text size="sm" c="red">{op.error}</Text> : null}
    </form>
  );
}
