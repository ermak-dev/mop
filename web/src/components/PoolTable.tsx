// Папеты по проектам (#298): секция на проект с заголовком-счётчиком и
// таблицей в тех же колонках, что на старой странице. Строка красится по
// корзине, которую посчитал сервер.
import { Badge, Code, Stack, Table, Text, Title, Tooltip } from "@mantine/core";
import type { Counts, Project, Puppet } from "../types";
import { KINDS } from "../types";
import { gb, KIND_COLOR, KIND_ONE } from "./format";

export const HEAD = ["папет", "узел", "аллокация", "состояние", "пользователь", "модель", "место", "репозиторий"];

export function projectNote(counts: Counts): string {
  return KINDS.filter((k) => counts[k]).map((k) => `${counts[k]} ${KIND_ONE[k]}`).join(", ");
}

function Row({ p }: { p: Puppet }) {
  const color = KIND_COLOR[p.kind];
  return (
    <Table.Tr data-kind={p.kind}>
      <Table.Td>
        <Badge size="xs" circle color={color} mr={6} />
        {p.name}
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
      <Table.Td ta="right">{gb(p.disk_kb)}</Table.Td>
      <Table.Td><Code>{p.origin}</Code></Table.Td>
    </Table.Tr>
  );
}

export function PoolTable({ projects }: { projects: Project[] }) {
  if (!projects.length) return <Text c="dimmed">папетов нет</Text>;
  return (
    <Stack gap="md" data-testid="pool">
      {projects.map((sh) => (
        <div key={sh.name}>
          <Title order={3}>
            {sh.name} <Text span c="dimmed" size="sm" fw={400}>{projectNote(sh.counts)}</Text>
          </Title>
          <Table striped highlightOnHover withTableBorder>
            <Table.Thead>
              <Table.Tr>{HEAD.map((h) => <Table.Th key={h} ta={h === "место" ? "right" : undefined}>{h}</Table.Th>)}</Table.Tr>
            </Table.Thead>
            <Table.Tbody>{sh.puppets.map((p) => <Row key={p.name} p={p} />)}</Table.Tbody>
          </Table>
        </div>
      ))}
    </Stack>
  );
}
