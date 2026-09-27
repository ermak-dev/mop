// Приложение дашборда (#296): шапка со счётчиками корзин, ошибки сборщика и
// секции -- пул и узлы (#298), расход (#299), кредиты (#300), журнал (#298).
import { Badge, Container, Group, Stack, Text, Title, Alert } from "@mantine/core";
import { usePool } from "./usePool";
import { PoolTable } from "./components/PoolTable";
import { NodesPanel } from "./components/NodesPanel";
import { Masters } from "./components/Masters";
import { Journal } from "./components/Journal";
import type { Counts } from "./types";
import { UsageChart } from "./components/UsageChart";
import { UsageByUser } from "./components/UsageByUser";
import { Credentials } from "./components/Credentials";
import { KIND_COLOR, KIND_WORD, plural, presentKinds } from "./components/format";
import { ThemeSwitch } from "./components/ThemeSwitch";

// Слова, цвета корзин и склонение -- одна таблица на страницу (#301, #323):
// components/format.ts.

export function Chips({ counts }: { counts: Counts }) {
  return (
    <Group gap="xs" data-testid="chips">
      <Badge size="lg" variant="light" color="gray">{plural(counts.puppets, "папет", "папета", "папетов")}</Badge>
      {presentKinds(counts).map((k) => (
        <Badge key={k} size="lg" variant="light" color={KIND_COLOR[k]}>{counts[k]} {KIND_WORD[k].many}</Badge>
      ))}
    </Group>
  );
}

export default function App() {
  const { snapshot } = usePool();
  return (
    <Container size="xl" py="md">
      <Stack gap="md">
        <Group justify="space-between" align="center">
          <Group gap="sm">
            <img src="/logo.png" alt="" width={28} height={28} />
            <Title order={2}>Master Of Puppet</Title>
          </Group>
          <Group gap="md">
            {snapshot ? <Chips counts={snapshot.counts} /> : <Text c="dimmed">снимок ещё не пришёл</Text>}
            <ThemeSwitch />
          </Group>
        </Group>
        {snapshot && snapshot.errors.length > 0 && (
          <Alert color="red" variant="light" title="сборщик">{snapshot.errors.join("\n")}</Alert>
        )}
        {snapshot && (
          <>
            <PoolTable projects={snapshot.projects} />
            {/* живые мастера (#305) */}
            <Masters masters={snapshot.masters} />
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
  );
}
