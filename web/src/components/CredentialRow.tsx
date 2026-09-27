// Строка реестра (#300): те же колонки, что у `mop cred list` и старой
// страницы; статус словами и его вид уже собраны сервером (#325), здесь
// только цвет по виду и время сброса квоты (#331, #326).
import { Badge, Table, Text } from "@mantine/core";
import type { Cred } from "../types";
import { AuthorizeFlow } from "./AuthorizeFlow";
import { canAuthorize } from "./creds-api";
import { credStatusText, listOrDash, percentText, statusColor } from "./format";
import { align } from "./TableHead";

// Колонки реестра (#324): подписи и выравнивание -- одной записью на заголовок
// и ячейку. «использовано» справа и там и там (прежде заголовок был слева).
export const CRED_COLS = {
  name: { label: "имя" }, profile: { label: "провайдер" }, kind: { label: "вид" },
  owner: { label: "владелец" }, status: { label: "статус" },
  used: { label: "использовано", right: true }, age: { label: "возраст" },
  holders: { label: "держатели" }, auth: { label: "" },
};

export function CredentialRow({ cred }: { cred: Cred }) {
  return (
    <Table.Tr data-testid={`cred-${cred.name}`}>
      <Table.Td><Text fw={500}>{cred.name}</Text></Table.Td>
      <Table.Td>{cred.profile}</Table.Td>
      <Table.Td>{cred.kind}</Table.Td>
      <Table.Td>{cred.owner || "-"}</Table.Td>
      {/* время сброса -- в поясе браузера, из resets_at (#331) */}
      <Table.Td><Badge variant="light" color={statusColor(cred.status_kind)}>
        {credStatusText(cred.status, cred.status_kind, cred.resets_at)}
      </Badge></Table.Td>
      <Table.Td ta={align(CRED_COLS.used)}>{percentText(cred.percent)}</Table.Td>
      <Table.Td>{cred.age}</Table.Td>
      <Table.Td>{listOrDash(cred.holders)}</Table.Td>
      <Table.Td>{canAuthorize(cred) ? <AuthorizeFlow name={cred.name} /> : null}</Table.Td>
    </Table.Tr>
  );
}
