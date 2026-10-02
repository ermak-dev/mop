// Диалог полного события (#422): вся запись, которую отдал сервер, -- на
// столе. Список полей строится из самой записи: известные пять рисуются
// строками в прежнем порядке, любые другие (#424 расширяет конверт) --
// дальше как есть, ничего не теряется и не выдумывается. Mantine Modal
// держит фокус внутри, Escape и крест закрывают.
import { Box, Code, Modal, Stack, Text } from "@mantine/core";
import type { JournalEntry } from "../types";
import { entryTitle, fullAt, journalText } from "../journalView";

/** Поля сегодняшней записи: подпись -> значение. received_at (#424) --
 *  когда сервер принял запись; есть не всегда (окно раскатки). */
function knownRows(e: JournalEntry): [string, string][] {
  const rows: [string, string][] = [
    ["время события", fullAt(e.at)],
    ["узел", e.node],
    ["папет", e.name],
    ["проект", e.project],
    ["событие", e.event],
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
  const extras = extraRows(entry);
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
      </Stack>
    </Modal>
  );
}
