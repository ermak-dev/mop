// Кто сколько тратит -- по пользователям (#299): таблица снимка `per_user`
// в порядке сервера (от самого прожорливого, user_rows). Колонки и подписи
// -- как на прежней странице; «-» -- неприписанный расход.
import { Paper, Table, Text, Title } from "@mantine/core";
import type { UsageUser } from "../types";
import { human } from "./format";

export const USER_COLUMNS = ["пользователь", "вход", "выход", "кэш зап.", "кэш чт.", "всего"] as const;

export function UsageByUser({ users }: { users: UsageUser[] }) {
  return (
    <Paper withBorder p="md" radius="md" data-testid="usage-by-user">
      <Title order={4}>
        Кто сколько тратит — по пользователям{" "}
        <Text span c="dimmed" size="sm" fw={400}>чьё сообщение начало ход; «-» — неприписанное</Text>
      </Title>
      {users.length ? (
        <Table striped highlightOnHover mt="sm" fz="sm">
          <Table.Thead>
            <Table.Tr>
              {USER_COLUMNS.map((c, i) => (
                <Table.Th key={c} ta={i ? "right" : "left"}>{c}</Table.Th>
              ))}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {users.map((u) => (
              <Table.Tr key={u.login}>
                <Table.Td>{u.login}</Table.Td>
                <Table.Td ta="right">{human(u.input)}</Table.Td>
                <Table.Td ta="right">{human(u.output)}</Table.Td>
                <Table.Td ta="right">{human(u.cache_write)}</Table.Td>
                <Table.Td ta="right">{human(u.cache_read)}</Table.Td>
                <Table.Td ta="right" fw={600}>{human(u.total)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <Text c="dimmed" size="sm" mt="sm">расход по пользователям появится после первого разбора транскриптов</Text>
      )}
    </Paper>
  );
}
