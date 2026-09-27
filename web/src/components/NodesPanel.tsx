// Узлы кластера (#298): те же колонки, что на старой странице; строка
// красится по состоянию узла (ready — свободен, draining/closed — занят,
// прочее — не работает).
import { Table, Text, Title } from "@mantine/core";
import type { Node } from "../types";
import { gbOfMb, KIND_COLOR, nodeKind, ratio } from "./format";

export const HEAD = ["узел", "драйвер", "проекты", "состояние", "свободно", "всего", "слоты"];

export function NodesPanel({ nodes }: { nodes: Node[] }) {
  return (
    <div data-testid="nodes">
      <Title order={3}>
        Узлы <Text span c="dimmed" size="sm" fw={400}>{nodes.length ? `${nodes.length} в кластере` : ""}</Text>
      </Title>
      {nodes.length ? (
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>{HEAD.map((h, i) => <Table.Th key={h} ta={i >= 4 ? "right" : undefined}>{h}</Table.Th>)}</Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {nodes.map((n) => {
              const kind = nodeKind(n.state);
              return (
                <Table.Tr key={n.name} data-kind={kind}>
                  <Table.Td>{n.name}</Table.Td>
                  <Table.Td>{n.driver}</Table.Td>
                  <Table.Td>{n.serves}</Table.Td>
                  <Table.Td><Text span c={KIND_COLOR[kind]}>{n.state}</Text></Table.Td>
                  <Table.Td ta="right">{gbOfMb(n.free_mb)}</Table.Td>
                  <Table.Td ta="right">{gbOfMb(n.total_mb)}</Table.Td>
                  <Table.Td ta="right">{ratio(n.slots, n.slots_total)}</Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      ) : <Text c="dimmed">пусто</Text>}
    </div>
  );
}
