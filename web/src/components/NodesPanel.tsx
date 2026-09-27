// Узлы кластера (#298): те же колонки, что на старой странице; строка
// красится по корзине узла, которую считает сервер (kind, #325/#326):
// строку state страница не разбирает.
import { Table, Text } from "@mantine/core";
import type { Node } from "../types";
import { gbOfMb, KIND_COLOR, ratio } from "./format";
import { EmptyNote, SectionTitle } from "./Section";
import { align, TableHead } from "./TableHead";

export const COLS = {
  name: { label: "узел" }, driver: { label: "драйвер" }, serves: { label: "проекты" },
  state: { label: "состояние" }, free: { label: "свободно", right: true },
  total: { label: "всего", right: true }, slots: { label: "слоты", right: true },
};

export function NodesPanel({ nodes }: { nodes: Node[] }) {
  return (
    <div data-testid="nodes">
      <SectionTitle title="Узлы" note={nodes.length ? `${nodes.length} в кластере` : ""} />
      {nodes.length ? (
        <Table striped withTableBorder>
          <TableHead cols={Object.values(COLS)} />
          <Table.Tbody>
            {nodes.map((n) => {
              const kind = n.kind;
              return (
                <Table.Tr key={n.name} data-kind={kind}>
                  <Table.Td>{n.name}</Table.Td>
                  <Table.Td>{n.driver}</Table.Td>
                  <Table.Td>{n.serves}</Table.Td>
                  <Table.Td><Text span c={kind && KIND_COLOR[kind]}>{n.state}</Text></Table.Td>
                  <Table.Td ta={align(COLS.free)}>{gbOfMb(n.free_mb)}</Table.Td>
                  <Table.Td ta={align(COLS.total)}>{gbOfMb(n.total_mb)}</Table.Td>
                  <Table.Td ta={align(COLS.slots)}>{ratio(n.slots, n.slots_total)}</Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      ) : <EmptyNote>пусто</EmptyNote>}
    </div>
  );
}
