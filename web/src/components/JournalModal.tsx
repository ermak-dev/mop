// Диалог полного события (#422): вся запись, которую отдал сервер, -- на
// столе. Список полей строится из самой записи: известные рисуются
// строками, любые другие (#424 расширяет конверт) -- дальше как есть,
// ничего не теряется и не выдумывается. Запись без имени/узла/проекта
// (oversized-маркер #424) рисуется прочерками, не undefined. Mantine Modal
// держит фокус внутри, Escape и крест закрывают; кнопка копирует запись
// целиком в буфер -- разбирать длинное событие удобнее в редакторе.
import { Box, Button, Code, Modal, Stack, Text } from "@mantine/core";
import { useState } from "react";
import type { JournalEntry } from "../types";
import { entryTitle, fullAt, journalText } from "../journalView";

/** Поле записи или прочерк: oversized-маркер #424 полей не несёт. */
function orDash(v: unknown): string {
  const s = typeof v === "object" ? JSON.stringify(v) : String(v ?? "");
  return s === "" || s === "undefined" || s === "null" ? "-" : s;
}

/** Поля сегодняшней записи: подпись -> значение. received_at (#424) --
 *  когда сервер принял запись; есть не всегда (окно раскатки). */
function knownRows(e: JournalEntry): [string, string][] {
  const rows: [string, string][] = [
    ["время события", fullAt(e.at)],
    ["узел", orDash(e.node)],
    ["папет", orDash(e.name)],
    ["проект", orDash(e.project)],
    ["событие", orDash(e.event)],
  ];
  if (e.received_at != null) rows.splice(1, 0, ["принято сервером", fullAt(e.received_at)]);
  return rows;
}

/** Прочие поля записи (#424): что сервер ни даст, без выдумки своих. */
function extraRows(e: JournalEntry): [string, string][] {
  return Object.entries(e)
    .filter(([k]) => !["at", "received_at", "event", "name", "node", "project", "text"].includes(k))
    .map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : String(v)]);
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <Text size="sm" style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
      <Text span c="dimmed" mr={8}>{label}:</Text>
      <Box component="span" style={{ verticalAlign: "top" }}>
        {value.includes("\n") ? <Code block>{value}</Code> : value}
      </Box>
    </Text>
  );
}

export function JournalModal({ entry, onClose }: { entry: JournalEntry; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const extras = extraRows(entry);
  const copy = async () => {
    // запись уже безопасна (сервер #424): копируем как отдал сервер
    try {
      await navigator.clipboard.writeText(JSON.stringify(entry, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);   // буфер недоступен (нет https/нет права) -- тихо
    }
  };
  return (
    <Modal opened onClose={onClose} title={entryTitle(entry)} centered size="lg"
           aria-label={entryTitle(entry)}>
      <Stack gap={6}>
        {knownRows(entry).map(([label, value]) => <Row key={label} label={label} value={value} />)}
        <Text size="sm" style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
          <Text span c="dimmed" mr={8}>описание:</Text>
          {journalText(entry)}
        </Text>
        {extras.map(([label, value]) => <Row key={label} label={label} value={value} />)}
        <Box mt="xs">
          <Button size="xs" variant="light" onClick={copy}
                  aria-label="копировать событие">{copied ? "скопировано" : "копировать"}</Button>
        </Box>
      </Stack>
    </Modal>
  );
}
