// Приложение дашборда (#296): шапка со счётчиками корзин, ошибки сборщика и
// секции -- пул и узлы (#298), расход (#299), журнал (#298).
import { Box, Container, Group, Stack, Text, Title, Alert } from "@mantine/core";
import { useEffect, useState } from "react";
import { usePool } from "./usePool";
import { fetchSession, GUEST, LoginRefused, postLogin, postLogout, type Session } from "./useSession";
import { SessionBox } from "./components/SessionBox";
import { PoolTable } from "./components/PoolTable";
import { NodesPanel } from "./components/NodesPanel";
import { Masters } from "./components/Masters";
import { Journal } from "./components/Journal";
import { UsageChart } from "./components/UsageChart";
import { UsageByUser } from "./components/UsageByUser";
import { LoadBadges, LoadBar } from "./components/PoolLoad";
import { ThemeSwitch } from "./components/ThemeSwitch";

// Слова, цвета корзин и склонение -- одна таблица на страницу (#301, #323):
// components/format.ts. Бейджи шапки и полоса аллокаций -- места пула от
// сервера (#378): components/PoolLoad.tsx.

export default function App() {
  const { snapshot } = usePool();
  // Сессия (#416): до ответа -- гость, 401/сбой панели не рвут публичные
  // таблицы; истёкшая cookie -- тот же гость.
  const [session, setSession] = useState<Session>(GUEST);
  const [failed, setFailed] = useState<string | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);
  useEffect(() => {
    fetchSession().then(setSession).catch(() => setPanelError("панель недоступна"));
  }, []);

  const login = async (login: string, password: string) => {
    try {
      setSession(await postLogin(login, password));
      setFailed(null);
    } catch (e) {
      // 401 -- сервер отказал, 429 -- лимит попыток; остальное -- панель
      setFailed(e instanceof LoginRefused
        ? (e.status === 401 ? "неверный логин или пароль" : e.reason)
        : "панель недоступна");
    }
  };
  const logout = async () => {
    try {
      setSession(await postLogout());
      setFailed(null);
    } catch {
      setPanelError("панель недоступна");
    }
  };
  // Полоса аллокаций -- во всю ширину окна (#378), поэтому страница -- два
  // контейнера: шапка над полосой, секции под ней.
  return (
    <>
      <Container size="xl" pt="md" pb="sm">
        <Group justify="space-between" align="center">
          <Group gap="sm">
            <img src="/logo.png" alt="" width={28} height={28} />
            <Title order={2}>Master Of Puppet</Title>
          </Group>
          <Group gap="md">
            {snapshot ? <LoadBadges load={snapshot.load} counts={snapshot.counts} />
              : <Text c="dimmed">снимок ещё не пришёл</Text>}
            {panelError && <Text c="red" size="sm">{panelError}</Text>}
            <SessionBox session={session} onLogin={login} onLogout={logout} failed={failed} />
            <ThemeSwitch />
          </Group>
        </Group>
      </Container>
      {/* снимок без load (сервер до #378) -- без полосы */}
      {snapshot?.load && <Box w="100%"><LoadBar load={snapshot.load} /></Box>}
      <Container size="xl" pt="sm" pb="md">
        <Stack gap="md">
          {snapshot && snapshot.errors.length > 0 && (
            <Alert color="red" variant="light" title="сборщик">{snapshot.errors.join("\n")}</Alert>
          )}
          {snapshot && (
            <>
              <PoolTable projects={snapshot.projects} />
              {/* живые мастера (#305) */}
              <Masters masters={snapshot.masters} every={snapshot.masters_every} />
              <NodesPanel nodes={snapshot.nodes} />
              {/* расход (#299) */}
              <UsageChart days={snapshot.usage} />
              <UsageByUser users={snapshot.per_user} />
              {/* кредиты (#300) */}
              <Journal journal={snapshot.journal} />
            </>
          )}
        </Stack>
      </Container>
    </>
  );
}
