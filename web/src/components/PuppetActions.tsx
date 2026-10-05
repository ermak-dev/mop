import { useState } from "react";
import { Group } from "@mantine/core";
import { IconTerminal2, IconFileText } from "@tabler/icons-react";
import { PuppetCard, type PuppetSource } from "./PuppetCard";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { IconButton } from "./IconButton";
import { ModelBox } from "./ModelBox";
import { RestartBox } from "./RestartBox";
import { UpdateBox } from "./UpdateBox";
// Разрушительные (#512): монтирование #473 в строку цели -- тот же
// контракт session+row, что у соседей; внутри компонента свой
// двухшаговый поток (preview -> подтверждение), общим он не становится.
import { DestructiveBox } from "./DestructiveBox";

export function PuppetActions({ row, session }: { row: Puppet; session: Session }) {
  const [viewer, setViewer] = useState<PuppetSource | null>(null);
  if (session.role !== "admin") return null;
  return <Group gap={4} wrap="nowrap" role="group" aria-label={`действия ${row.name}`}
    style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
    <IconButton label="экран" variant="subtle" size="lg" onClick={() => setViewer("tail")}>
      <IconTerminal2 size={18} />
    </IconButton>
    <IconButton label="лог" variant="subtle" size="lg" onClick={() => setViewer("stderr")}>
      <IconFileText size={18} />
    </IconButton>
    {viewer && <PuppetCard session={session} target={row.name} which={viewer} onClose={() => setViewer(null)} />}
    <ModelBox session={session} row={row} />
    <UpdateBox session={session} row={row} />
    <RestartBox session={session} row={row} />
    <DestructiveBox session={session} row={row} />
  </Group>;
}
