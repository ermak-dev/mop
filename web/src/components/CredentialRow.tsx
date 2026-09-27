// Строка реестра (#300): те же колонки, что у `mop cred list` и старой
// страницы; статус словами уже собран сервером, здесь только цвет.
import { Badge, Table, Text } from "@mantine/core";
import type { Cred } from "../types";
import { AuthorizeFlow } from "./AuthorizeFlow";
import { canAuthorize } from "./creds-api";
import { listOrDash, percentText, statusColor } from "./format";

export function CredentialRow({ cred }: { cred: Cred }) {
  return (
    <Table.Tr data-testid={`cred-${cred.name}`}>
      <Table.Td><Text fw={500}>{cred.name}</Text></Table.Td>
      <Table.Td>{cred.profile}</Table.Td>
      <Table.Td>{cred.kind}</Table.Td>
      <Table.Td>{cred.owner || "-"}</Table.Td>
      <Table.Td><Badge variant="light" color={statusColor(cred.status)}>{cred.status}</Badge></Table.Td>
      <Table.Td ta="right">{percentText(cred.percent)}</Table.Td>
      <Table.Td>{cred.age}</Table.Td>
      <Table.Td>{listOrDash(cred.holders)}</Table.Td>
      <Table.Td>{canAuthorize(cred) ? <AuthorizeFlow name={cred.name} /> : null}</Table.Td>
    </Table.Tr>
  );
}
