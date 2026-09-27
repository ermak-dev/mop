// Кто сколько тратит -- по пользователям (#299): таблица снимка `per_user`
// в порядке сервера (от самого прожорливого, user_rows). Колонки и подписи
// -- как на прежней странице; «-» -- неприписанный расход.
import { Paper, Table } from "@mantine/core";
import type { UsageUser } from "../types";
import { human } from "./format";
import { EmptyNote, SectionTitle } from "./Section";
import { align, TableHead } from "./TableHead";

export const USER_COLS = {
  login: { label: "пользователь" }, input: { label: "вход", right: true },
  output: { label: "выход", right: true }, cache_write: { label: "кэш зап.", right: true },
  cache_read: { label: "кэш чт.", right: true }, total: { label: "всего", right: true },
};

export function UsageByUser({ users }: { users: UsageUser[] }) {
  return (
    <Paper withBorder p="md" radius="md" data-testid="usage-by-user">
      <SectionTitle order={4} title="Кто сколько тратит — по пользователям"
        note="чьё сообщение начало ход; «-» — неприписанное" />
      {users.length ? (
        <Table striped highlightOnHover mt="sm" fz="sm">
          <TableHead cols={Object.values(USER_COLS)} />
          <Table.Tbody>
            {users.map((u) => (
              <Table.Tr key={u.login}>
                <Table.Td>{u.login}</Table.Td>
                <Table.Td ta={align(USER_COLS.input)}>{human(u.input)}</Table.Td>
                <Table.Td ta={align(USER_COLS.output)}>{human(u.output)}</Table.Td>
                <Table.Td ta={align(USER_COLS.cache_write)}>{human(u.cache_write)}</Table.Td>
                <Table.Td ta={align(USER_COLS.cache_read)}>{human(u.cache_read)}</Table.Td>
                <Table.Td ta={align(USER_COLS.total)} fw={600}>{human(u.total)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <EmptyNote size="sm" mt="sm">расход по пользователям появится после первого разбора транскриптов</EmptyNote>
      )}
    </Paper>
  );
}
