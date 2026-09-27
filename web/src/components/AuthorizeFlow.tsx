// Вход в claude из строки реестра (#300, прежде #294 на старой странице).
//
// Вкладка под страницу входа открывается синхронно в обработчике клика,
// пока жест пользователя жив: окно, открытое через двадцать секунд, когда
// сервер вернёт адрес, блокировщик всплывающих окон отбросит. Адрес
// подставляется в неё потом; ссылка в строке остаётся запасной, если
// вкладку закрыли. Начатый вход живёт в состоянии компонента, а не в DOM,
// и переживает перерисовку снимка.
import { useState } from "react";
import { ActionIcon, Group, Loader, Text, TextInput, Tooltip } from "@mantine/core";
import { IconExternalLink, IconLogin2, IconSend } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { loginCode, loginStart } from "./creds-api";

interface Started { url: string }

export function AuthorizeFlow({ name }: { name: string }) {
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState<Started | null>(null);
  const [code, setCode] = useState("");

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
      notifications.show({ color: "red", title: name, message: r.error || "отказ" });
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
      notifications.show({ color: "red", title: name, message: r.error || "отказ" });
    }
  }

  if (!started) {
    // Кнопки -- иконки с подсказкой (#304): текстовая кнопка рядом с
    // бейджем статуса читалась как ещё один бейдж.
    return (
      <Tooltip label="авторизоваться" withArrow>
        <ActionIcon variant="subtle" size="md" onClick={start} loading={busy}
          aria-label="авторизоваться" data-testid={`auth-${name}`}>
          <IconLogin2 size={18} stroke={1.8} />
        </ActionIcon>
      </Tooltip>
    );
  }
  return (
    <Group gap="xs" wrap="nowrap">
      <Tooltip label="открыть страницу входа" withArrow>
        <ActionIcon component="a" href={started.url} target="_blank" rel="noopener" variant="subtle" size="md"
          aria-label="открыть страницу входа">
          <IconExternalLink size={18} stroke={1.8} />
        </ActionIcon>
      </Tooltip>
      <TextInput size="xs" placeholder="код со страницы" value={code} onChange={(e) => setCode(e.currentTarget.value)}
        aria-label={`код для ${name}`} onKeyDown={(e) => { if (e.key === "Enter" && code.trim() && !busy) submit(); }} />
      <Tooltip label="отправить код" withArrow>
        <ActionIcon variant="filled" size="md" onClick={submit} disabled={!code.trim() || busy} aria-label="отправить код">
          <IconSend size={16} stroke={1.8} />
        </ActionIcon>
      </Tooltip>
      {busy ? <Loader size="xs" /> : <Text size="xs" c="dimmed">войдите на открывшейся странице, вставьте код</Text>}
    </Group>
  );
}
