// Секция «Кредиты» (#300): реестр на сервере, секреты страница не видит;
// добавление и удаление -- команды mop cred, со страницы только вход в claude.
import { Table } from "@mantine/core";
import type { Cred } from "../types";
import { CRED_COLS, CredentialRow } from "./CredentialRow";
import { EmptyNote, SectionTitle } from "./Section";
import { TableHead } from "./TableHead";

export function Credentials({ creds }: { creds: Cred[] }) {
  return (
    <section>
      <SectionTitle title="Кредиты"
        note="авторизации у провайдеров LLM; реестр на сервере, секреты страница не видит" />
      {creds.length === 0 ? (
        <EmptyNote size="sm">кредитов нет — mop cred add или mop cred login</EmptyNote>
      ) : (
        <Table striped highlightOnHover withTableBorder>
          <TableHead cols={Object.values(CRED_COLS)} />
          <Table.Tbody>{creds.map((c) => <CredentialRow key={c.name} cred={c} />)}</Table.Tbody>
        </Table>
      )}
    </section>
  );
}
