// Папеты по проектам (#298): секция на проект с заголовком-счётчиком.
// #452: статус -- одна колонка: слово по виду вердикта, цвет по корзине,
// подробность -- под словом; аллокация ушла из таблицы в подсказку (данные
// её по-прежнему несут). #478: ветка -- своя колонка (чип ветки внутри
// статуса ломал чтение колонки скопом), репозиторий уехал из таблицы в
// заголовок секции: секция и есть проект, у всех строк один origin.
import { Badge, Box, Code, Group, NativeSelect, Stack, Table, Text, Tooltip } from "@mantine/core";
import { useState } from "react";
import type { Counts, LocalSession, Master, Project, Puppet, RosterSession } from "../types";
import { roleKey, roleLabel, roster } from "../roster";
import type { Session } from "../useSession";
import { PuppetActions } from "./PuppetActions";
import { gb, KIND_WORD, presentKinds, verdictColor, verdictWord } from "./format";
import { EmptyNote, SectionTitle } from "./Section";
import { align, TableHead } from "./TableHead";

export const COLS = {
  name: { label: "папет" }, role: { label: "Role" }, node: { label: "узел" },
  state: { label: "статус" }, branch: { label: "ветка" },
  owner: { label: "пользователь" }, llm: { label: "модель" },
  disk: { label: "место", right: true },
};

export function projectNote(counts: Counts): string {
  return presentKinds(counts).map((k) => `${counts[k]} ${KIND_WORD[k].one}`).join(", ");
}

// Подсказка статуса (#452): полная state-строка, какой её собрал сервер, и
// аллокация -- то, что ушло из таблицы, но спрашивается взглядом.
const statusTip = (p: Puppet): string => `${p.state} · ${p.alloc_status}`;

// Репозитории секции (#478): по одному на каждую строку, без повторов.
// Группировка сервера даёт один; список, а не первое значение, -- чтобы
// перемену контракта было видно, а не молча потерять второй origin.
const origins = (entries: RosterSession[]): string[] =>
  [...new Set(entries.flatMap((entry) => entry.source === "managed" && entry.origin ? [entry.origin] : []))];

function Row({ p, role, onOpen, session }: {
  p: Puppet; role: string | null; onOpen?: (name: string) => void; session?: Session;
}) {
  const color = verdictColor(p.kind, p.bucket);
  return (
    <Table.Tr data-kind={p.bucket || p.kind} data-source="managed">
      <Table.Td>
        {/* точка корзины перед именем, в одну строку: узкая колонка иначе
            переносила имя под точку. Имя -- кнопка при наличии карточки
            (#463): открытие доступно и с клавиатуры (Tab + Enter). */}
        <Group gap={6} wrap="nowrap">
          <Badge size="xs" circle color={color} />
          {onOpen ? (
            <Text component="button" span size="sm" title="карточка папета"
                  style={{ whiteSpace: "nowrap", background: "none", border: 0,
                           padding: 0, cursor: "pointer", textAlign: "left", font: "inherit" }}
                  onClick={() => onOpen(p.name)}>{p.name}</Text>
          ) : (
            <Text span size="sm" style={{ whiteSpace: "nowrap" }}>{p.name}</Text>
          )}
        </Group>
      </Table.Td>
      <Table.Td><Text size="sm" c={role ? undefined : "dimmed"}>{role || "Unassigned"}</Text></Table.Td>
      <Table.Td>{p.node}</Table.Td>
      <Table.Td>
        <Tooltip label={statusTip(p)} disabled={p.state.length <= 60} multiline w={420}>
          <Box>
            <Group gap={6} wrap="nowrap">
              <Text span c={color} fw={p.bucket === "free" || p.bucket === "busy" ? 400 : 600}
                    lineClamp={1}>{verdictWord(p.kind)}</Text>
            </Group>
            {/* Подробность -- под словом, тем же цветом: причина видна без
                наведения, «not responding», «prove», «uncommitted: 3». */}
            {p.detail ? <Text span size="xs" c="dimmed" lineClamp={1}>{p.detail}</Text> : null}
          </Box>
        </Tooltip>
      </Table.Td>
      {/* Ветка -- своей колонкой из своего поля (#478): сравнивается между
          строками, не мешая читать статус по колонке скопом. */}
      <Table.Td>{p.branch ? <Code>{p.branch}</Code> : "-"}</Table.Td>
      <Table.Td>{p.owner || "-"}</Table.Td>
      <Table.Td>{p.llm}</Table.Td>
      <Table.Td ta={align(COLS.disk)}>{gb(p.disk_kb)}</Table.Td>
      {session?.role === "admin" && <Table.Td ta="right" style={{ width: "1%", whiteSpace: "nowrap" }}>
        <PuppetActions key={`${session.login}\0${p.name}`} row={p} session={session} />
      </Table.Td>}
    </Table.Tr>
  );
}

function LocalRow({ entry, admin }: { entry: LocalSession; admin: boolean }) {
  return <Table.Tr data-source="local">
    <Table.Td>
      <Text size="sm">{entry.display}</Text>
      {entry.cwd && <Text size="xs" c="dimmed" maw={260} truncate title={entry.cwd}>{entry.cwd}</Text>}
    </Table.Td>
    <Table.Td><Text size="sm" c="dimmed">Legacy local</Text></Table.Td>
    <Table.Td>-</Table.Td>
    <Table.Td>
      <Text size="sm" c="dimmed">{entry.identified ? "Local session" : "MCP endpoint"}</Text>
      {entry.addresses.map((address) => <div key={address}><Code>{address}</Code></div>)}
    </Table.Td>
    <Table.Td>-</Table.Td>
    <Table.Td>{entry.user || "-"}</Table.Td>
    <Table.Td>-</Table.Td>
    <Table.Td ta="right">-</Table.Td>
    {admin && <Table.Td />}
  </Table.Tr>;
}

export function PoolTable({ projects, sessions, masters, onOpen, session }: {
  projects: Project[];
  sessions?: RosterSession[];
  masters?: Master[];
  session?: Session;
  /** Открыть можно только управляемую строку; локальные сессии не имеют
   *  Nomad-цели, даже когда зритель — администратор. */
  onOpen?: (name: string) => void;
}) {
  const [role, setRole] = useState("");
  const entries = roster(projects, sessions, masters);
  const keys = [...new Set(entries.map(roleKey))].sort();
  if (role && !keys.includes(role)) keys.push(role);
  const visible = entries.filter((entry) => !role || roleKey(entry) === role);
  const groups = new Map<string, RosterSession[]>();
  for (const entry of visible) {
    const group = groups.get(entry.project) ?? [];
    group.push(entry);
    groups.set(entry.project, group);
  }
  const admin = session?.role === "admin";
  return (
    <Stack gap="md" data-testid="pool">
      <Group justify="space-between" align="end">
        <NativeSelect label="Role" w="100%" maw={260} value={role}
          data={[{ value: "", label: "All roles" }, ...keys.map((key) => ({ value: key, label: roleLabel(key) }))]}
          onChange={(event) => setRole(event.currentTarget.value)} />
        <Text size="sm" c="dimmed">{visible.length} entries</Text>
      </Group>
      {!visible.length && <EmptyNote>{entries.length ? "No sessions match this role" : "No sessions"}</EmptyNote>}
      {[...groups].map(([name, group]) => {
        const counts = projects.find((project) => project.name === name)?.counts;
        const locals = group.filter((entry) => entry.source === "local").length;
        const note = role ? `${group.length} entries` :
          [counts ? projectNote(counts) : "", locals ? `${locals} local entries` : ""].filter(Boolean).join(", ");
        return <div key={name}>
          <SectionTitle title={name} note={note} />
          {origins(group).map((origin) => (
            <Text key={origin} size="sm" c="dimmed"><Code>{origin}</Code></Text>
          ))}
          <Table.ScrollContainer minWidth={admin ? 1160 : 900}>
            <Table striped highlightOnHover withTableBorder>
              <TableHead cols={[...Object.values(COLS), ...(admin ? [{ label: "действия", right: true, compact: true }] : [])]} />
              <Table.Tbody>{group.map((entry) => entry.source === "local"
                ? <LocalRow key={entry.id} entry={entry} admin={admin} />
                : <Row key={entry.id} p={{ ...entry, owner: entry.user || "", origin: entry.origin || "?" }}
                    role={entry.role} onOpen={onOpen} session={session} />)}</Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </div>;
      })}
    </Stack>
  );
}
