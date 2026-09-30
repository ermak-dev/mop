// Приложение дашборда (#296): шапка со счётчиками корзин, ошибки сборщика и
// секции -- пул и узлы (#298), расход (#299), кредиты (#300), журнал (#298).
import { Box, Container, Group, Stack, Text, Title, Alert } from "@mantine/core";
import { usePool } from "./usePool";
import { PoolTable } from "./components/PoolTable";
import { NodesPanel } from "./components/NodesPanel";
import { Masters } from "./components/Masters";
import { Journal } from "./components/Journal";
import { UsageChart } from "./components/UsageChart";
import { UsageByUser } from "./components/UsageByUser";
import { Credentials } from "./components/Credentials";
import { LoadBadges, LoadBar } from "./components/PoolLoad";
import { ThemeSwitch } from "./components/ThemeSwitch";

// Слова, цвета корзин и склонение -- одна таблица на страницу (#301, #323):
// components/format.ts. Бейджи шапки и полоса аллокаций -- места пула от
// сервера (#378): components/PoolLoad.tsx.

export default function App() {
  const { snapshot } = usePool();
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
              <Credentials creds={snapshot.creds} />
              <Journal journal={snapshot.journal} />
            </>
          )}
        </Stack>
      </Container>
    </>
  );
}
