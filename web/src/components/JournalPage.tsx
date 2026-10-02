// Страница журнала (#422): все события шины на одном экране для админа --
// полная дата/время, событие, папет, узел, проект и описание; строка
// открывается кликом или с клавиатуры (кнопка в ячейке времени -- вся
// строка доступна Tab'ом и Enter'ом). Новые сверху; снимок не хранится --
// что пришло из useJournal, то и на экране.
import { useEffect, useRef, useState } from "react";
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
// Ячейки -- строками: full_entry хранит произвольный JSON, и объект в
// name/event/node ронял таблицу («Objects are not valid as a React child»,
// ревью 7); сырое значение остаётся в диалоге.
function Row({ e, onOpen }: { e: JournalEntry; onOpen: () => void }) {
  const cell = (v: unknown) => (v == null ? "-" : typeof v === "object" ? JSON.stringify(v) : String(v));
  return (
    <Table.Tr onClick={onOpen} onKeyDown={(ev) => { if (ev.key === "Enter") onOpen(); }}>
      <Table.Td>
        <UnstyledButton onClick={onOpen} style={{ fontVariantNumeric: "tabular-nums" }}>
          {fullAt(e.at)}
        </UnstyledButton>
      </Table.Td>
      <Table.Td><Text span fw={600}>{cell(e.event)}</Text></Table.Td>
      <Table.Td>{cell(e.name)}</Table.Td>
      <Table.Td>{cell(e.node)}</Table.Td>
      <Table.Td>{cell(e.project)}</Table.Td>
      <Table.Td><Text lineClamp={1}>{journalText(e)}</Text></Table.Td>
    </Table.Tr>
  );
}

export function JournalPage({ journal, error = null, open, initial, sourceKey }: {
  journal: JournalEntry[];
  /** Отказ источника (#424): null -- загрузка прошла или не начиналась. */
  error?: string | null;
  /** Открыть запись (страница-владелец держит состояние диалога). */
  open: (e: JournalEntry) => void;
  /** Запись, чей диалог уже открыт (переход по ссылке внутрь). */
  initial?: JournalEntry;
  /** Ключ источника (кто смотрит): смена закрывает диалог, опрос -- нет. */
  sourceKey: string;
}) {
  const [entry, setEntry] = useState<JournalEntry | null>(initial ?? null);
  const items = [...journal].reverse();   // новые сверху
  // Открытый диалог не переживает отказ источника (ревью 2 #422): error --
  // право ушло, и скрыть надо в этот же рендер, не эффектом после paint:
  // рендер с пустым списком уже не рисует ни таблицы, ни модала.
  const denied = error != null;
  // и не переживает смену источника (ревью 3): журнал другого админа --
  // запись A в диалоге чужая. Ключ источника приносит App (кто смотрит),
  // а не ссылка массива: опрос строит новый массив тем же содержимым
  // каждые POLL_S, и на ссылке диалог закрывался бы сам собой (ревью 4).
  const prevSource = useRef(sourceKey);
  const switched = prevSource.current !== sourceKey;
  useEffect(() => {
    if (denied || switched) setEntry(null);
    prevSource.current = sourceKey;
  }, [denied, switched, sourceKey]);
  const shown = denied ? [] : items;
  const held = denied || switched ? null : entry;
  return (
    <div data-testid="journal-page">
      <SectionTitle title="Журнал" note={shown.length ? "события шины, новые сверху" : undefined} />
      {error && <Text c="red" size="sm" mb="xs">{error}</Text>}
      {shown.length ? (
        <Table striped highlightOnHover withTableBorder>
          <Table.Thead>
            <Table.Tr>{Object.values(COLS).map((c, i) => <Table.Th key={i}>{c.label}</Table.Th>)}</Table.Tr>
          </Table.Thead>
          <Table.Tbody>{shown.map((e, i) => <Row key={i} e={e} onOpen={() => { setEntry(e); open(e); }} />)}</Table.Tbody>
        </Table>
      ) : !error && <EmptyNote>{EMPTY}</EmptyNote>}
      {/* отказ или смена источника скрыли таблицу этим же рендером; модал
          держится только на показанном журнале */}
      {held && <JournalModal entry={held} onClose={() => setEntry(null)} />}
    </div>
  );
}
