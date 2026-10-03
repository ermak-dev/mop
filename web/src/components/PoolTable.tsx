// Папеты по проектам (#298): секция на проект с заголовком-счётчиком.
// #452: статус -- одна колонка: слово по виду вердикта, цвет по корзине,
// ветка -- чипом рядом, подробность -- под словом; аллокация ушла из
// таблицы в подсказку (данные её по-прежнему несут).
import { Badge, Box, Code, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import type { Counts, Project, Puppet } from "../types";
import { gb, KIND_WORD, presentKinds, verdictColor, verdictWord } from "./format";
import { EmptyNote, SectionTitle } from "./Section";
import { align, TableHead } from "./TableHead";

export const COLS = {
  name: { label: "папет" }, node: { label: "узел" },
  state: { label: "статус" }, owner: { label: "пользователь" }, llm: { label: "модель" },
  disk: { label: "место", right: true }, origin: { label: "репозиторий" },
};

export function projectNote(counts: Counts): string {
  return presentKinds(counts).map((k) => `${counts[k]} ${KIND_WORD[k].one}`).join(", ");
}

// Подсказка статуса (#452): полная state-строка, какой её собрал сервер, и
// аллокация -- то, что ушло из таблицы, но спрашивается взглядом.
const statusTip = (p: Puppet): string => `${p.state} · ${p.alloc_status}`;

function Row({ p }: { p: Puppet }) {
  const color = verdictColor(p.kind, p.bucket);
  return (
    <Table.Tr data-kind={p.bucket || p.kind}>
      <Table.Td>
        {/* точка корзины перед именем, в одну строку: узкая колонка иначе
            переносила имя под точку */}
        <Group gap={6} wrap="nowrap">
          <Badge size="xs" circle color={color} />
          <Text span size="sm" style={{ whiteSpace: "nowrap" }}>{p.name}</Text>
        </Group>
      </Table.Td>
      <Table.Td>{p.node}</Table.Td>
      <Table.Td>
        <Tooltip label={statusTip(p)} disabled={p.state.length <= 60} multiline w={420}>
          <Box>
            <Group gap={6} wrap="nowrap">
              <Text span c={color} fw={p.bucket === "free" || p.bucket === "busy" ? 400 : 600}
                    lineClamp={1}>{verdictWord(p.kind)}</Text>
              {/* Ветка -- отдельным чипом из своего поля (#452): страница
                  не разбирает state-строку. */}
              {p.branch ? <Code>{p.branch}</Code> : null}
            </Group>
            {/* Подробность -- под словом, тем же цветом: причина видна без
                наведения, «not responding», «prove», «uncommitted: 3». */}
            {p.detail ? <Text span size="xs" c="dimmed" lineClamp={1}>{p.detail}</Text> : null}
          </Box>
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
