// #507: кнопки «лог» (stderr) больше нет -- живые сессии в tmux, stderr у
// задачи пуст; причина падающей уже в вердикте строки (#126). Экран остаётся.
import { useState } from "react";
import { Group } from "@mantine/core";
import { IconTerminal2 } from "@tabler/icons-react";
import { PuppetCard } from "./PuppetCard";
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
  // экран или ничего (#507): второй источник -- stderr -- ушёл с кнопкой «лог»
  const [viewer, setViewer] = useState<"tail" | null>(null);
  if (session.role !== "admin") return null;
  return <Group gap={4} wrap="nowrap" role="group" aria-label={`действия ${row.name}`}
    style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
    <IconButton label="экран" variant="subtle" size="lg" onClick={() => setViewer("tail")}>
      <IconTerminal2 size={18} />
    </IconButton>
    {viewer && <PuppetCard session={session} target={row.name} onClose={() => setViewer(null)} />}
    <ModelBox session={session} row={row} />
    <UpdateBox session={session} row={row} />
    <RestartBox session={session} row={row} />
    <DestructiveBox session={session} row={row} />
  </Group>;
}
