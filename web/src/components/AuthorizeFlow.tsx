// Вход в claude из строки реестра (#300, прежде #294 на старой странице).
//
// Вкладка под страницу входа открывается синхронно в обработчике клика,
// пока жест пользователя жив: окно, открытое через двадцать секунд, когда
// сервер вернёт адрес, блокировщик всплывающих окон отбросит. Адрес
// подставляется в неё потом; ссылка в строке остаётся запасной, если
// вкладку закрыли. Начатый вход живёт в состоянии компонента, а не в DOM,
// и переживает перерисовку снимка.
import { useState } from "react";
import { Anchor, Button, Group, Loader, Text, TextInput } from "@mantine/core";
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
    return (
      <Button size="compact-xs" variant="light" onClick={start} loading={busy} data-testid={`auth-${name}`}>
        Авторизоваться
      </Button>
    );
  }
  return (
    <Group gap="xs" wrap="nowrap">
      <Anchor href={started.url} target="_blank" rel="noopener" size="xs">открыть страницу входа</Anchor>
      <TextInput size="xs" placeholder="код со страницы" value={code} onChange={(e) => setCode(e.currentTarget.value)}
        aria-label={`код для ${name}`} />
      <Button size="compact-xs" onClick={submit} disabled={!code.trim() || busy}>Отправить код</Button>
      {busy ? <Loader size="xs" /> : <Text size="xs" c="dimmed">войдите на открывшейся странице, вставьте код</Text>}
    </Group>
  );
}
