// Вход в claude из строки реестра (#300, прежде #294 на старой странице).
//
// Вкладка под страницу входа открывается синхронно в обработчике клика,
// пока жест пользователя жив: окно, открытое через двадцать секунд, когда
// сервер вернёт адрес, блокировщик всплывающих окон отбросит. Адрес
// подставляется в неё потом; ссылка в строке остаётся запасной, если
// вкладку закрыли. Начатый вход живёт в состоянии компонента, а не в DOM,
// и переживает перерисовку снимка.
import { useState } from "react";
import { Group, Loader, Text, TextInput } from "@mantine/core";
import { IconExternalLink, IconLogin2, IconSend } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { loginCode, loginStart, type Reply } from "./creds-api";
import { IconButton } from "./IconButton";

interface Started { url: string }

export function AuthorizeFlow({ name }: { name: string }) {
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState<Started | null>(null);
  const [code, setCode] = useState("");
  // Отказ сервера -- одним уведомлением у старта и у кода.
  const refused = (r: Reply) => notifications.show({ color: "red", title: name, message: r.error || "отказ" });

  async function start() {
    const tab = window.open("", "_blank");        // до запроса, см. выше
    setBusy(true);
    const r = await loginStart(name);
    setBusy(false);
    if (r.ok && r.url) {
      if (tab) tab.location.href = r.url;
      setStarted({ url: r.url });
    } else {
      tab?.close();
      refused(r);
    }
  }

  async function submit() {
    setBusy(true);
    const r = await loginCode(name, code);
    setBusy(false);
    if (r.ok) {
      notifications.show({ color: "green", title: name, message: r.owner ? `вошёл как ${r.owner}` : "вошёл" });
      setStarted(null);
      setCode("");
    } else {
      refused(r);
    }
  }

  if (!started) {
    // Кнопки -- иконки с подсказкой (#304): текстовая кнопка рядом с
    // бейджем статуса читалась как ещё один бейдж.
    return (
      <IconButton label="авторизоваться" variant="subtle" size="md" onClick={start} loading={busy}
        data-testid={`auth-${name}`}>
        <IconLogin2 size={18} stroke={1.8} />
      </IconButton>
    );
  }
  return (
    <Group gap="xs" wrap="nowrap">
      <IconButton label="открыть страницу входа" href={started.url} variant="subtle" size="md">
        <IconExternalLink size={18} stroke={1.8} />
      </IconButton>
      <TextInput size="xs" placeholder="код со страницы" value={code} onChange={(e) => setCode(e.currentTarget.value)}
        aria-label={`код для ${name}`} onKeyDown={(e) => { if (e.key === "Enter" && code.trim() && !busy) submit(); }} />
      <IconButton label="отправить код" variant="filled" size="md" onClick={submit} disabled={!code.trim() || busy}>
        <IconSend size={16} stroke={1.8} />
      </IconButton>
      {busy ? <Loader size="xs" /> : <Text size="xs" c="dimmed">войдите на открывшейся странице, вставьте код</Text>}
    </Group>
  );
}
