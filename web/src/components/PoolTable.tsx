// Папеты по проектам (#298): секция на проект с заголовком-счётчиком.
// #452: статус -- одна колонка: слово по виду вердикта, цвет по корзине,
// подробность -- под словом; аллокация ушла из таблицы в подсказку (данные
// её по-прежнему несут). #478: ветка -- своя колонка (чип ветки внутри
// статуса ломал чтение колонки скопом), репозиторий уехал из таблицы в
// заголовок секции: секция и есть проект, у всех строк один origin.
import { Badge, Box, Code, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import type { Counts, Project, Puppet } from "../types";
import type { Session } from "../useSession";
import { PuppetActions } from "./PuppetActions";
import { gb, KIND_WORD, presentKinds, verdictColor, verdictWord } from "./format";
import { EmptyNote, SectionTitle } from "./Section";
import { align, TableHead } from "./TableHead";

export const COLS = {
  name: { label: "папет" }, node: { label: "узел" },
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
const origins = (sh: Project): string[] =>
  [...new Set(sh.puppets.map((p) => p.origin).filter(Boolean))];

function Row({ p, onOpen, session }: {
  p: Puppet; onOpen?: (name: string) => void; session?: Session;
}) {
  const color = verdictColor(p.kind, p.bucket);
  return (
    <Table.Tr data-kind={p.bucket || p.kind}>
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
      {session?.role === "admin" && <Table.Td>
        <PuppetActions key={`${session.login}\0${p.name}`} row={p} session={session} />
      </Table.Td>}
    </Table.Tr>
  );
}

export function PoolTable({ projects, onOpen, session }: {
  projects: Project[];
  session?: Session;
  /** Открыть карточку папета по имени (#463); нет -- имя строкой, гостю
   *  карточка недоступна и опенер не рисуется. */
  onOpen?: (name: string) => void;
}) {
  if (!projects.length) return <EmptyNote>папетов нет</EmptyNote>;
  return (
    <Stack gap="md" data-testid="pool">
      {projects.map((sh) => (
        <div key={sh.name}>
          {/* Репозиторий -- в заголовке (#478): секция сгруппирована по
              project_of(origin), у всех строк он один, колонка повторяла
              бы его в каждой строке. Разные origin внутри секции не
              допускаются группировкой; на всякий случай показываем все. */}
          <SectionTitle title={sh.name} note={projectNote(sh.counts)} />
          {origins(sh).map((o) => (
            <Text key={o} size="sm" c="dimmed"><Code>{o}</Code></Text>
          ))}
          <Table.ScrollContainer minWidth={session?.role === "admin" ? 1080 : 800}>
            <Table striped highlightOnHover withTableBorder>
              <TableHead cols={[...Object.values(COLS), ...(session?.role === "admin" ? [{ label: "действия" }] : [])]} />
              <Table.Tbody>{sh.puppets.map((p) => <Row key={p.name} p={p} onOpen={onOpen} session={session} />)}</Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </div>
      ))}
    </Stack>
  );
}
