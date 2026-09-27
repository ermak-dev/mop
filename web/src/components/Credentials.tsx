// Секция «Кредиты» (#300): реестр на сервере, секреты страница не видит;
// добавление и удаление -- команды mop cred, со страницы только вход в claude.
import { Table, Text, Title } from "@mantine/core";
import type { Cred } from "../types";
import { CredentialRow } from "./CredentialRow";

const HEAD = ["имя", "провайдер", "вид", "владелец", "статус", "использовано", "возраст", "держатели", ""];

export function Credentials({ creds }: { creds: Cred[] }) {
  return (
    <section>
      <Title order={3}>
        Кредиты{" "}
        <Text span size="sm" c="dimmed">авторизации у провайдеров LLM; реестр на сервере, секреты страница не видит</Text>
      </Title>
      {creds.length === 0 ? (
        <Text c="dimmed" size="sm">кредитов нет — mop cred add или mop cred login</Text>
      ) : (
        <Table striped highlightOnHover withTableBorder>
          <Table.Thead>
            <Table.Tr>{HEAD.map((h, i) => <Table.Th key={i}>{h}</Table.Th>)}</Table.Tr>
          </Table.Thead>
          <Table.Tbody>{creds.map((c) => <CredentialRow key={c.name} cred={c} />)}</Table.Tbody>
        </Table>
      )}
    </section>
  );
}
