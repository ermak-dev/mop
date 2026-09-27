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

const POLL = "опрос who раз в 30 с";

export function Masters({ masters }: { masters: Master[] }) {
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
