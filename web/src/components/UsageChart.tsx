// Токены по дням (#299): столбики @mantine/charts по оси снимка `usage`
// (все дни окна, включая пустые -- их даёт сервер, usage_axis). Заголовок
// несёт сумму за окно, как на прежней странице: «за 14 дней, всего 836M».
import { BarChart } from "@mantine/charts";
import { Paper, Text, Title } from "@mantine/core";
import type { UsageDay } from "../types";
import { human, plural } from "./format";

export function usageNote(days: UsageDay[]): string {
  if (!days.length) return "ещё не собрано";
  const grand = days.reduce((a, d) => a + d.total, 0);
  return `за ${plural(days.length, "день", "дня", "дней")}, всего ${human(grand)}`;
}

export function UsageChart({ days }: { days: UsageDay[] }) {
  return (
    <Paper withBorder p="md" radius="md" data-testid="usage-chart">
      <Title order={4}>
        Токены по дням <Text span c="dimmed" size="sm" fw={400}>{usageNote(days)}</Text>
      </Title>
      {days.length ? (
        <BarChart
          h={220}
          mt="sm"
          data={days.map((d) => ({ date: d.date, tokens: d.total }))}
          dataKey="date"
          series={[{ name: "tokens", label: "токены", color: "blue.6" }]}
          valueFormatter={(v) => (v ? human(v) : "-")}
          withLegend={false}
          gridAxis="y"
        />
      ) : (
        <Text c="dimmed" size="sm" mt="sm">расход появится после первого разбора транскриптов</Text>
      )}
    </Paper>
  );
}
