// Приложение дашборда (#296): шапка со счётчиками корзин, ошибки сборщика и
// секции -- пул и узлы (#298), расход (#299), кредиты (#300), журнал (#298).
import { Badge, Container, Group, Stack, Text, Title, Alert } from "@mantine/core";
import { usePool } from "./usePool";
import { PoolTable } from "./components/PoolTable";
import { NodesPanel } from "./components/NodesPanel";
import { Journal } from "./components/Journal";
import { KINDS, type Counts, type Kind } from "./types";
import { UsageChart } from "./components/UsageChart";
import { UsageByUser } from "./components/UsageByUser";
import { Credentials } from "./components/Credentials";
import { KIND_COLOR } from "./components/format";
import { ThemeSwitch } from "./components/ThemeSwitch";

export const KIND_RU: Record<Kind, string> = {
  free: "свободны", busy: "заняты", sick: "больны", silent: "агент молчит", down: "не подняты",
};
// Цвета корзин -- одна таблица на страницу (#301): components/format.ts.

export function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return `${n} ${one}`;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return `${n} ${few}`;
  return `${n} ${many}`;
}

export function Chips({ counts }: { counts: Counts }) {
  return (
    <Group gap="xs" data-testid="chips">
      <Badge size="lg" variant="light" color="gray">{plural(counts.puppets, "папет", "папета", "папетов")}</Badge>
      {KINDS.filter((k) => counts[k]).map((k) => (
        <Badge key={k} size="lg" variant="light" color={KIND_COLOR[k]}>{counts[k]} {KIND_RU[k]}</Badge>
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
