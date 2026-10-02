// Живые мастера (#305, #423). Строка -- живая Claude-сессия (#421):
// сессия отвечает с нескольких адресов (личный инбокс и общий проекта), и
// все они -- в одной строке. Реестра мастеров нет намеренно: живой -- тот,
// кто отозвался на опрос who. Адрес -- тот, по которому мастеру пишут send.
// identified (#421): true -- строка склеена сервером по сессии, false --
// legacy endpoint: имя сессии получено, но за ним может стоять сколько
// угодно сессий, и счётчиком сессий оно не является.
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

// Число без вранья (#423): подтверждённые сессии и адреса без идентификатора
// считаются раздельно; старый снимок без identified -- все адреса.
const count = (masters: Master[]) => {
  const sessions = masters.filter((m) => m.identified).length;
  const endpoints = masters.length - sessions;
  const what = (n: number, one: string, many: string) =>
    `${n} ${n % 10 === 1 && n % 100 !== 11 ? one : many}`;
  // Ноль сессий при ненулевых адресах -- тоже цифра: старый снимок не
  // выдаётся за сессии молчанием нуля.
  const parts = [what(sessions, "сессия", "сессий")];
  if (endpoints) parts.push(`${endpoints} по адресам`);
  return parts.join(", ");
};

// Адреса строки: список сессии (#421), у старого снимка -- сам master.
const addressesOf = (m: Master) => (m.addresses?.length ? m.addresses : [m.master]);

export function Masters({ masters, every }: { masters: Master[]; every?: number }) {
  const POLL = poll(every);
  return (
    <div data-testid="masters">
      <SectionTitle title="Мастера" note={masters.length ? `${count(masters)}; ${POLL}` : POLL} />
      {masters.length ? (
        <Table striped withTableBorder fz="sm" mt="xs">
          <TableHead cols={COLS} />
          <Table.Tbody>
            {masters.map((m) => (
              <Table.Tr key={`${m.project}/${m.master}`}>
                <Table.Td>{m.project}</Table.Td>
                <Table.Td>{m.user}</Table.Td>
                <Table.Td>
                  {m.session}
                  {/* Legacy endpoint (#423): имя получено, сессия не
                      подтверждена -- не masquerade as a session. */}
                  {m.identified ? null : (
                    <Text span size="sm" c="dimmed"> (MCP-адрес, сессия не подтверждена)</Text>
                  )}
                </Table.Td>
                <Table.Td><Text span size="sm" c="dimmed">{m.cwd}</Text></Table.Td>
                <Table.Td>
                  {addressesOf(m).map((a) => (
                    <div key={a}><Code>{a}</Code></div>
                  ))}
                </Table.Td>
                <Table.Td>{listOrDash(m.puppets)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : <EmptyNote size="sm">{EMPTY}</EmptyNote>}
    </div>
  );
}
