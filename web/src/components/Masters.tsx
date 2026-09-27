// Живые мастера (#305). Реестра мастеров нет намеренно: сборщик сервиса
// опрашивает who в общий инбокс каждого проекта, как инструмент agents, и
// живой -- тот, кто отозвался. Адрес -- тот, по которому мастеру пишут send.
import { Code, Table, Text } from "@mantine/core";
import type { Master } from "../types";
import { listOrDash } from "./format";
import { EmptyNote, SectionTitle } from "./Section";
import { TableHead } from "./TableHead";

export const EMPTY = "мастеров на шине нет — мастер отвечает, пока жив его MCP-сервер mop";

export const COLS = [
  { label: "проект" }, { label: "пользователь" }, { label: "сессия" },
  { label: "каталог" }, { label: "адрес" }, { label: "папеты" },
];

// Период -- из снимка (masters_every, #325/#326), а не своей копией числа;
// снимок без него -- без числа, а не с выдуманным.
const poll = (every?: number) => (every ? `опрос who раз в ${every} с` : "опрос who");

export function Masters({ masters, every }: { masters: Master[]; every?: number }) {
  const POLL = poll(every);
  return (
    <div data-testid="masters">
      <SectionTitle title="Мастера" note={masters.length ? `${masters.length} на шине; ${POLL}` : POLL} />
      {masters.length ? (
        <Table striped withTableBorder fz="sm" mt="xs">
          <TableHead cols={COLS} />
          <Table.Tbody>
            {masters.map((m) => (
              <Table.Tr key={`${m.project}/${m.master}`}>
                <Table.Td>{m.project}</Table.Td>
                <Table.Td>{m.user}</Table.Td>
                <Table.Td>{m.session}</Table.Td>
                <Table.Td><Text span size="sm" c="dimmed">{m.cwd}</Text></Table.Td>
                <Table.Td><Code>{m.master}</Code></Table.Td>
                <Table.Td>{listOrDash(m.puppets)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : <EmptyNote size="sm">{EMPTY}</EmptyNote>}
    </div>
  );
}
