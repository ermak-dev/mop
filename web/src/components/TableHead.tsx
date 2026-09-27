// Колонки таблиц страницы (#324): подпись и выравнивание -- одной записью,
// и заголовок, и ячейка читают его оттуда. Прежде заголовок строился из
// списка подписей, а `ta="right"` ставился ячейкам отдельно, и они
// разъехались: «использовано» у кредитов стояло справа в ячейке и слева в
// заголовке.
import { Table } from "@mantine/core";

export interface Col { label: string; right?: boolean }

/** Выравнивание колонки для Table.Th и Table.Td. */
export const align = (col: Col) => (col.right ? "right" : undefined);

export function TableHead({ cols }: { cols: readonly Col[] }) {
  return (
    <Table.Thead>
      <Table.Tr>{cols.map((c, i) => <Table.Th key={i} ta={align(c)}>{c.label}</Table.Th>)}</Table.Tr>
    </Table.Thead>
  );
}
