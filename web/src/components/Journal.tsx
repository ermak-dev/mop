// Журнал событий шины (#298): новые сверху, та же строка, что на старой
// странице: время, событие, папет, узел и проект, текст.
import { Stack, Text } from "@mantine/core";
import type { JournalEntry } from "../types";
import { hhmmss } from "./format";
import { EmptyNote, SectionTitle } from "./Section";

export const EMPTY = "событий пока нет — агенты пишут send, idle, type, wipe и свой запуск";

// Сколько событий рисует страница (#303, просьба оператора 27.09). Сервис
// помнит больше (EVENTS_CAP в mop/server/web.py) и отдаёт их в /api/pool.
export const JOURNAL_LIMIT = 25;

export function Journal({ journal }: { journal: JournalEntry[] }) {
  const items = journal.slice(-JOURNAL_LIMIT).reverse();
  const base = "события шины, новые сверху";
  const note = journal.length > JOURNAL_LIMIT ? `${base}; ${JOURNAL_LIMIT} из ${journal.length}` : base;
  return (
    <div data-testid="journal">
      <SectionTitle title="Журнал" note={note} />
      {items.length ? (
        <Stack gap={2}>
          {items.map((e, i) => (
            <Text key={i} size="sm" style={{ borderTop: "1px solid var(--mantine-color-default-border)", padding: "3px 0" }}>
              <Text span c="dimmed" mr={8} style={{ fontVariantNumeric: "tabular-nums" }}>{hhmmss(e.at)}</Text>
              <Text span fw={600} mr={6}>{e.event}</Text>
              {e.name}{" "}
              <Text span c="dimmed">на {e.node}, проект {e.project}</Text>{" "}
              {e.text}
            </Text>
          ))}
        </Stack>
      ) : <EmptyNote>{EMPTY}</EmptyNote>}
    </div>
  );
}
