// Папеты по проектам (#298): секция на проект с заголовком-счётчиком и
// таблицей в тех же колонках, что на старой странице. Строка красится по
// корзине, которую посчитал сервер.
import { Badge, Code, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import type { Counts, Project, Puppet } from "../types";
import { gb, KIND_COLOR, KIND_WORD, presentKinds } from "./format";
import { EmptyNote, SectionTitle } from "./Section";
import { align, TableHead } from "./TableHead";

export const COLS = {
  name: { label: "папет" }, node: { label: "узел" }, alloc: { label: "аллокация" },
  state: { label: "состояние" }, owner: { label: "пользователь" }, llm: { label: "модель" },
  disk: { label: "место", right: true }, origin: { label: "репозиторий" },
};

export function projectNote(counts: Counts): string {
  return presentKinds(counts).map((k) => `${counts[k]} ${KIND_WORD[k].one}`).join(", ");
}

function Row({ p }: { p: Puppet }) {
  const color = KIND_COLOR[p.kind];
  return (
    <Table.Tr data-kind={p.kind}>
      <Table.Td>
        {/* точка корзины перед именем, в одну строку: узкая колонка иначе
            переносила имя под точку */}
        <Group gap={6} wrap="nowrap">
          <Badge size="xs" circle color={color} />
          <Text span size="sm" style={{ whiteSpace: "nowrap" }}>{p.name}</Text>
        </Group>
      </Table.Td>
      <Table.Td>{p.node}</Table.Td>
      <Table.Td>{p.alloc_status}</Table.Td>
      <Table.Td>
        <Tooltip label={p.state} disabled={p.state.length <= 60} multiline w={420}>
          <Text span c={color} fw={p.kind === "free" || p.kind === "busy" ? 400 : 600} lineClamp={1}>{p.state}</Text>
        </Tooltip>
      </Table.Td>
      <Table.Td>{p.owner || "-"}</Table.Td>
      <Table.Td>{p.llm}</Table.Td>
      <Table.Td ta={align(COLS.disk)}>{gb(p.disk_kb)}</Table.Td>
      <Table.Td><Code>{p.origin}</Code></Table.Td>
    </Table.Tr>
  );
}

export function PoolTable({ projects }: { projects: Project[] }) {
  if (!projects.length) return <EmptyNote>папетов нет</EmptyNote>;
  return (
    <Stack gap="md" data-testid="pool">
      {projects.map((sh) => (
        <div key={sh.name}>
          <SectionTitle title={sh.name} note={projectNote(sh.counts)} />
          <Table striped highlightOnHover withTableBorder>
            <TableHead cols={Object.values(COLS)} />
            <Table.Tbody>{sh.puppets.map((p) => <Row key={p.name} p={p} />)}</Table.Tbody>
          </Table>
        </div>
      ))}
    </Stack>
  );
}
