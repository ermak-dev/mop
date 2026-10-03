// Карточка папета (#463): приватная панель админа над строкой ростера.
// Факты -- тот же formatter и те же слова, что в таблице (#452): своего
// перечня статусов здесь нет. Хвост экрана и stderr -- по явной кнопке,
// без follow: строки показываются текстом (React экранирует, ANSI не
// разбирается и не исполняется), обрезание сервер назвал -- показано.
// Ничего не пишется в storage; закрытие -- с клавиатуры (focus trap у
// Mantine Modal, закрытие -- Escape и кнопка).
import { useState } from "react";
import { Code, Group, Loader, Modal, Stack, Table, Text } from "@mantine/core";
import type { Snapshot } from "../types";
import type { Session } from "../useSession";
import { usePuppetCard, usePuppetTail } from "../usePuppetCard";
import { RestartBox } from "./RestartBox";
import { SendForm } from "./SendForm";
import { UpdateBox } from "./UpdateBox";
import { gb, verdictColor, verdictWord } from "./format";
import { TableHead } from "./TableHead";

const COLS = {
  fact: { label: "факт" }, value: { label: "значение" },
};

/** Одна строка фактов: значение -- текстом, «-» без данных (#274: None на
 *  проводе -- данных нет, страница не гадает причину). */
function Fact({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <Table.Tr>
      <Table.Td><Text span c="dimmed">{k}</Text></Table.Td>
      <Table.Td>{v}</Table.Td>
    </Table.Tr>
  );
}

/** Строки хвоста: <pre> без разбора -- разметка и ANSI остаются текстом. */
function TailLines({ lines }: { lines: string[] }) {
  return (
    <Stack gap={4} data-testid="tail-lines">
      {lines.map((l, i) => (
        <Text key={i} component="pre" fz="xs" m={0} style={{
          whiteSpace: "pre-wrap", wordBreak: "break-all", overflowWrap: "anywhere",
        }}>{l === "" ? " " : l}</Text>
      ))}
    </Stack>
  );
}

export function PuppetCard({ snapshot, session, target, onClose }: {
  /** Снимок пула: карточка черпает владельца/работу из строк ростера. */
  snapshot: Snapshot;
  /** Кто смотрит: не-admin карточки не видит вовсе. */
  session: Session;
  /** Имя цели; null -- карточка закрыта. */
  target: string | null;
  onClose: () => void;
}) {
  const { card, error } = usePuppetCard(session, target);
  const [which, setWhich] = useState<"tail" | "stderr">("tail");
  const tail = usePuppetTail(session, target, which);
  // время наблюдения -- метка снимка, из которого ростер узнал цель
  const at = snapshot.at ? new Date(snapshot.at * 1000).toLocaleTimeString() : "-";
  const row = target ? card : null;
  // Цели нет -- модала нет вовсе: факты не остаются в DOM подо спрятанным
  // переходом (jsdom и медленные анимации оставили бы содержимое).
  if (!target) return null;
  return (
    <Modal opened onClose={onClose} size="lg"
           title={`папет ${target}`} data-testid="puppet-card"
           closeButtonProps={{ "aria-label": "закрыть" }}>
      {session.role !== "admin" ? null : error ? (
        <Text c="red">{error}</Text>
      ) : !row ? (
        <Group gap="sm"><Loader size="sm" /><Text c="dimmed">карточка грузится</Text></Group>
      ) : (
        <Stack gap="sm">
          <Group gap="sm">
            <Text span fw={600} c={verdictColor(row.kind, row.bucket)}>{verdictWord(row.kind)}</Text>
            {row.detail ? <Text span size="sm" c="dimmed">{row.detail}</Text> : null}
            <Text span size="sm" c="dimmed">наблюдение в {at}</Text>
          </Group>
          {/* рестарт (#465): кнопка и точное подтверждение строки; не-admin
              его не видит, смена цели гасит подтверждение вместе с карточкой */}
          <RestartBox session={session} row={row} />
          {/* обновление (#472): fresh -- явный выбор keep/replace/remove */}
          <UpdateBox session={session} row={row} />
          {/* адресный send (#469): тот же общий ход операции (useAdminOp);
              форма видна только админу -- не-admin внутрь не заходит */}
          {session.role === "admin" && <SendForm session={session} target={row.name} />}
          <Table striped withTableBorder>
            <TableHead cols={Object.values(COLS)} />
            <Table.Tbody>
              <Fact k="узел" v={row.node || "-"} />
              <Fact k="ветка" v={row.branch ? <Code>{row.branch}</Code> : "-"} />
              <Fact k="владелец" v={row.owner || "-"} />
              <Fact k="модель" v={row.llm || "-"} />
              <Fact k="репозиторий" v={row.origin ? <Code>{row.origin}</Code> : "-"} />
              <Fact k="место клона" v={gb(row.disk_kb)} />
              <Fact k="state-строка" v={<Code>{row.state}</Code>} />
            </Table.Tbody>
          </Table>
          <Group gap="sm">
            <TailButton label="экран" active={which === "tail"}
                        onClick={() => { setWhich("tail"); void tail.load(); }} />
            <TailButton label="stderr" active={which === "stderr"}
                        onClick={() => { setWhich("stderr"); void tail.load(); }} />
            <TailButton label="обновить" active={false} onClick={() => void tail.load()} />
          </Group>
          {tail.error ? <Text c="red">{tail.error}</Text> : null}
          {tail.truncated ? <Text size="sm" c="dimmed">обрезано: показан конец</Text> : null}
          {tail.lines.length ? <TailLines lines={tail.lines} /> : null}
        </Stack>
      )}
    </Modal>
  );
}

function TailButton({ label, active, onClick }: {
  label: string; active: boolean; onClick: () => void;
}) {
  return (
    <Text component="button" size="sm" fz="sm" fw={active ? 600 : 400}
          onClick={onClick} onKeyPress={onClick}
          style={{ background: "none", border: 0, cursor: "pointer", padding: 0 }}>
      {label}
    </Text>
  );
}
