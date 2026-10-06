// Форма адресного send внизу экрана папета,
// на том же общем ходе операции (useAdminOp): POST
// /api/admin/ops {action: "send", params: {message}} -> id -> вердикт.
// Политика первого выпуска #461: адресная доставка, никакого wait/notify
// в UI. Слова вердикта -- модель #476, как у RestartBox (opWord):
// succeeded -- «доставлено» только для send, остальное -- общий словарь.
// Payload живёт в поле на время полёта; в storage и URL его нет --
// запрет storage #463 держит и здесь.
import { useState } from "react";
import { Button, Group, Textarea } from "@mantine/core";
import type { Session } from "../useSession";
import { TERMINAL, useAdminOp } from "../useAdminOp";
import { useOperationNotice } from "../useOperationNotice";
import { opWord } from "./RestartBox";

/** Слово исхода send: свой глагол у «succeeded», остальное -- общий словарь. */
export const sendWord = (status: string): string =>
  status === "succeeded" ? "Доставлено" : opWord(status);

export function SendForm({ session, target }: { session: Session; target: string }) {
  // третий аргумент хука -- pollMs (#473): действие уезжает в submit, не сюда
  const { op, submit } = useAdminOp(session, session.role === "admin" ? target : null);
  const [message, setMessage] = useState("");
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  useOperationNotice(op, {
    scope: `${session.login}\0${session.role}\0${target}`,
    title: `Отправка: ${target}`, word: sendWord(op.status),
    enabled: session.role === "admin", notifySuccess: false,
  });

  const send = () => {
    if (!message.trim() || live) return;    // пустое не постится, живой ход один
    void submit("send", { message: message.trim() });
    setMessage("");                         // текст уехал: в поле его больше нет
  };

  return (
    <form data-testid="send-form" onSubmit={(e) => { e.preventDefault(); send(); }}>
      <Group align="flex-end" wrap="nowrap" gap="sm">
        <Textarea
          aria-label="Сообщение"
          placeholder="Сообщение папету"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "Enter" || e.shiftKey || e.altKey
                || e.nativeEvent.isComposing || e.keyCode === 229) return;
            e.preventDefault();
            if (!e.repeat) send();
          }}
          autosize minRows={1} maxRows={4}
          style={{ flex: 1, minWidth: 0 }}
          data-testid="send-text"
        />
        <Button type="submit" size="sm" loading={op.status === "submitted"}
                disabled={!message.trim() || live} data-testid="send-send">Отправить</Button>
      </Group>
    </form>
  );
}
