// Сессия в шапке дашборда (#416): гость -- Login и окно входа, оператор --
// Logout, admin -- ссылка на панель LLM-прокси. Вход и выход делает App
// (useSession), здесь только форма и кнопки; пароль живёт в поле на время
// попытки и после неё очищается -- ни в storage, ни в props.
import { useState } from "react";
import { Alert, Anchor, Button, Group, Modal, PasswordInput, TextInput } from "@mantine/core";
import { GUEST, type Session } from "../useSession";
import { LABELS } from "./labels";

type Props = {
  session: Session;
  /** true -- вход удался (окно закроется), false -- отказ (окно остаётся,
   *  рядом причина в failed). Не бросает: отказ входа -- не ошибка страницы. */
  onLogin: (login: string, password: string) => Promise<boolean>;
  onLogout: () => Promise<void>;
  /** Причина отказа входа с сервера (LoginRefused.reason), null -- нет отказа. */
  failed: string | null;
};

export function SessionBox({ session, onLogin, onLogout, failed }: Props) {
  const [open, setOpen] = useState(false);
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");

  // Закрытие модала -- тоже конец попытки: пароля в поле нет ни после
  // отправки, ни после «Отмена»/креста.
  const close = () => { setOpen(false); setPassword(""); };

  const submit = async () => {
    // После попытки -- успеха или отказа -- пароля в поле нет: он живёт
    // ровно на время попытки и никуда больше не записывается. Успех
    // закрывает окно; отказ оставляет открытым -- рядом причина.
    setPassword("");
    if (await onLogin(login, password)) setOpen(false);
  };

  if (session.login) {
    return (
      <Group gap="sm">
        {/* Только admin видит ссылку: панель прокси -- управление установки */}
        {session.role === "admin" && (
          <Anchor href="/llm/management.html" target="_blank" rel="noopener"
                  aria-label="LLM Proxy">LLM Proxy</Anchor>
        )}
        <span>{session.login}</span>
        <Button variant="light" color="gray" onClick={onLogout}>{LABELS.logout}</Button>
      </Group>
    );
  }
  return (
    <>
      <Button variant="light" onClick={() => setOpen(true)}>{LABELS.login}</Button>
      <Modal opened={open} onClose={close} title="Вход" centered>
        {failed && <Alert color="red" variant="light" mb="sm">{failed}</Alert>}
        <TextInput label="Логин" value={login} onChange={(e) => setLogin(e.target.value)} />
        <PasswordInput label="Пароль" value={password}
                       onChange={(e) => setPassword(e.target.value)} mt="sm" />
        <Group justify="flex-end" mt="md">
          <Button variant="light" color="gray" onClick={close}>{LABELS.cancel}</Button>
          <Button onClick={submit}>{LABELS.login}</Button>
        </Group>
      </Modal>
    </>
  );
}

/** Гость -- до первого ответа /api/session (App рисует то же, что гостю). */
export const BEFORE_FIRST_SESSION = GUEST;
