// Действия строки (#516): в строке -- «экран» и «правка»; все операции
// цели (правка, модель, рестарт, recycle/delete/wipe) -- пунктами в окне
// правки (ControlWindow). Волна #507-#513 натащила в строку восемь
// иконок; операторский минимализм свернул их в одно окно. Боксы
// остаются смонтированными здесь же: их useAdminOp-стейт переживает
// окно (вердикт, пришедший после закрытия, не теряется), окно лишь
// запускает поток выбранного пункта сигнал-токеном.
// #507: кнопка «лог» (stderr) ушла -- живые сессии в tmux, stderr у
// задачи пуст; экран остаётся.
import { useState } from "react";
import { Group } from "@mantine/core";
import { IconSettings, IconTerminal2 } from "@tabler/icons-react";
import { PuppetCard } from "./PuppetCard";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { IconButton } from "./IconButton";
import { ModelBox } from "./ModelBox";
import { RestartBox } from "./RestartBox";
import { UpdateBox } from "./UpdateBox";
import { DestructiveBox } from "./DestructiveBox";
// #512: монтирование #473 в строку цели -- тот же контракт session+row;
// #516: кожа иконок уехала в окно, поток остался тем же компонентом.
import { ControlWindow, type ControlEntry, type Signal } from "./ControlWindow";

export function PuppetActions({ row, session }: { row: Puppet; session: Session }) {
  // экран или ничего (#507): второй источник -- stderr -- ушёл с кнопкой «лог»
  const [viewer, setViewer] = useState<"tail" | null>(null);
  // окно правки (#516): перечень операций цели пунктами
  const [win, setWin] = useState(false);
  // сигналы пунктов (#516): токен меняется на каждый выбор, боксы
  // срабатывают по изменению
  const [sigUpdate, setSigUpdate] = useState<Signal>(null);
  const [sigModel, setSigModel] = useState<Signal>(null);
  const [sigRestart, setSigRestart] = useState<Signal>(null);
  const [sigRuin, setSigRuin] = useState<{ sub: string; at: number } | null>(null);
  if (session.role !== "admin") return null;
  const pick = (entry: ControlEntry) => {
    setWin(false);
    const at = { at: Date.now() };
    if (entry === "update") setSigUpdate(at);
    else if (entry === "model") setSigModel(at);
    else if (entry === "restart") setSigRestart(at);
    else setSigRuin({ sub: entry, at: Date.now() });
  };
  return <Group gap={4} wrap="nowrap" role="group" aria-label={`действия ${row.name}`}
    style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
    <IconButton label="экран" variant="subtle" size="lg" onClick={() => setViewer("tail")}>
      <IconTerminal2 size={18} />
    </IconButton>
    {viewer && <PuppetCard session={session} target={row.name} onClose={() => setViewer(null)} />}
    <IconButton label="правка" variant="subtle" size="lg" onClick={() => setWin(true)}>
      <IconSettings size={18} />
    </IconButton>
    {/* Боксы -- в коже пункта окна: без иконок, вход по сигналу. */}
    <ModelBox session={session} row={row} icon={false} signal={sigModel} />
    <UpdateBox session={session} row={row} icon={false} signal={sigUpdate} />
    <RestartBox session={session} row={row} icon={false} signal={sigRestart} />
    <DestructiveBox session={session} row={row} icon={false} signal={sigRuin} />
    <ControlWindow opened={win} onClose={() => setWin(false)} onPick={pick}
                   row={row} session={session} />
  </Group>;
}
