// Журнал событий шины (#298): новые сверху, та же строка, что на старой
// странице: время, событие, папет, узел и проект, текст.
import { Stack, Text, Title } from "@mantine/core";
import type { JournalEntry } from "../types";
import { hhmm } from "./format";

export const EMPTY = "событий пока нет — агенты пишут send, idle, type, wipe и свой запуск";

export function Journal({ journal }: { journal: JournalEntry[] }) {
  const items = journal.slice().reverse();
  return (
    <div data-testid="journal">
      <Title order={3}>
        Журнал <Text span c="dimmed" size="sm" fw={400}>события шины, новые сверху</Text>
      </Title>
      {items.length ? (
        <Stack gap={2}>
          {items.map((e, i) => (
            <Text key={i} size="sm" style={{ borderTop: "1px solid var(--mantine-color-default-border)", padding: "3px 0" }}>
              <Text span c="dimmed" mr={8} style={{ fontVariantNumeric: "tabular-nums" }}>{hhmm(e.at)}</Text>
              <Text span fw={600} mr={6}>{e.event}</Text>
              {e.name}{" "}
              <Text span c="dimmed">на {e.node}, проект {e.project}</Text>{" "}
              {e.text}
            </Text>
          ))}
        </Stack>
      ) : <Text c="dimmed">{EMPTY}</Text>}
    </div>
  );
}
