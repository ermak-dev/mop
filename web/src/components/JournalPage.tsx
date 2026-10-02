// Страница журнала (#422): все события шины на одном экране для админа --
// полная дата/время, событие, папет, узел, проект и описание; строка
// открывается кликом или с клавиатуры (кнопка в ячейке времени -- вся
// строка доступна Tab'ом и Enter'ом). Новые сверху; снимок не хранится --
// что пришло из useJournal, то и на экране.
import { useEffect, useState } from "react";
import { Table, Text, UnstyledButton } from "@mantine/core";
import type { JournalEntry } from "../types";
import { fullAt, journalText } from "../journalView";
import { EmptyNote, SectionTitle } from "./Section";
import { JournalModal } from "./JournalModal";

export const COLS = {
  at: { label: "время" }, event: { label: "событие" }, name: { label: "папет" },
  node: { label: "узел" }, project: { label: "проект" }, text: { label: "что случилось" },
};

export const EMPTY = "событий пока нет — агенты пишут send, idle, type, wipe и свой запуск";

// Время как кнопка: строка таблицы не фокусится сама, а кнопка в первой
// ячейке -- и клик мышью, и Tab+Enter; на клик по всей строке -- тоже открываем.
function Row({ e, onOpen }: { e: JournalEntry; onOpen: () => void }) {
  return (
    <Table.Tr onClick={onOpen} onKeyDown={(ev) => { if (ev.key === "Enter") onOpen(); }}>
      <Table.Td>
        <UnstyledButton onClick={onOpen} style={{ fontVariantNumeric: "tabular-nums" }}>
          {fullAt(e.at)}
        </UnstyledButton>
      </Table.Td>
      <Table.Td><Text span fw={600}>{e.event}</Text></Table.Td>
      <Table.Td>{e.name}</Table.Td>
      <Table.Td>{e.node}</Table.Td>
      <Table.Td>{e.project}</Table.Td>
      <Table.Td><Text lineClamp={1}>{journalText(e)}</Text></Table.Td>
    </Table.Tr>
  );
}

export function JournalPage({ journal, error = null, open, initial }: {
  journal: JournalEntry[];
  /** Отказ источника (#424): null -- загрузка прошла или не начиналась. */
  error?: string | null;
  /** Открыть запись (страница-владелец держит состояние диалога). */
  open: (e: JournalEntry) => void;
  /** Запись, чей диалог уже открыт (переход по ссылке внутрь). */
  initial?: JournalEntry;
}) {
  const [entry, setEntry] = useState<JournalEntry | null>(initial ?? null);
  const items = [...journal].reverse();   // новые сверху
  // Открытый диалог не переживает отказ источника (ревью #422): журнал --
  // только действующему админу, и error здесь значит, что право ушло, --
  // ни диалога, ни таблицы устаревших данных на экране.
  useEffect(() => { if (error) setEntry(null); }, [error]);
  return (
    <div data-testid="journal-page">
      <SectionTitle title="Журнал" note={items.length ? "события шины, новые сверху" : undefined} />
      {error && <Text c="red" size="sm" mb="xs">{error}</Text>}
      {items.length ? (
        <Table striped highlightOnHover withTableBorder>
          <Table.Thead>
            <Table.Tr>{Object.values(COLS).map((c, i) => <Table.Th key={i}>{c.label}</Table.Th>)}</Table.Tr>
          </Table.Thead>
          <Table.Tbody>{items.map((e, i) => <Row key={i} e={e} onOpen={() => { setEntry(e); open(e); }} />)}</Table.Tbody>
        </Table>
      ) : !error && <EmptyNote>{EMPTY}</EmptyNote>}
      {entry && <JournalModal entry={entry} onClose={() => setEntry(null)} />}
    </div>
  );
}
