// Живые мастера (#305). Реестра мастеров нет намеренно: сборщик сервиса
// опрашивает who в общий инбокс каждого проекта, как инструмент agents, и
// живой -- тот, кто отозвался. Адрес -- тот, по которому мастеру пишут send.
import { Code, Table, Text, Title } from "@mantine/core";
import type { Master } from "../types";
import { listOrDash } from "./format";

export const EMPTY = "мастеров на шине нет — мастер отвечает, пока жив его MCP-сервер mop";

export const HEAD = ["проект", "пользователь", "сессия", "каталог", "адрес", "папеты"];

export function Masters({ masters }: { masters: Master[] }) {
  return (
    <div data-testid="masters">
      <Title order={3}>
        Мастера{" "}
        <Text span c="dimmed" size="sm" fw={400}>
          {masters.length ? `${masters.length} на шине; опрос who раз в 30 с` : "опрос who раз в 30 с"}
        </Text>
      </Title>
      {masters.length ? (
        <Table striped withTableBorder fz="sm" mt="xs">
          <Table.Thead>
            <Table.Tr>{HEAD.map((h) => <Table.Th key={h}>{h}</Table.Th>)}</Table.Tr>
          </Table.Thead>
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
      ) : <Text c="dimmed" size="sm">{EMPTY}</Text>}
    </div>
  );
}
