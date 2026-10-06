// Действия строки (#516/#520): в строке -- «экран» и «правка»; все
// операции цели -- визардом ControlWindow (Stepper: выбор действия ->
// параметры -> подтверждение). Боксы-движки (useAdminOp +
// OperationResult/балуны) остаются смонтированными здесь же: их
// состояние переживает окно, вердикт, пришедший после закрытия, не
// теряется; визард шлёт им один приказ-токен на строку, каждый движок
// берёт свой act. «новая команда» в вердикте обновления возвращает
// визард на шаг обновления (повтор -- новый выбор режима).
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
import { ControlWindow, type ControlEntry, type Signal } from "./ControlWindow";

export function PuppetActions({ row, session }: { row: Puppet; session: Session }) {
  // экран или ничего (#507): второй источник -- stderr -- ушёл с кнопкой «лог»
  const [viewer, setViewer] = useState<"tail" | null>(null);
  // окно правки (#516/#520): визард операций цели
  const [win, setWin] = useState(false);
  // Возврат «новая команда» (#520): визард открывается сразу на шаге
  // обновления -- повтор обновления это новый выбор режима
  const [reopen, setReopen] = useState<ControlEntry | null>(null);
  // Приказ движкам (#520): один токен на строку, каждый берёт свой act
  const [order, setOrder] = useState<Signal>(null);
  // Состояния движков (#487): {act: status}. Визард при повторном входе
  // узнаёт из них, есть ли непоказанный ход, и приказывает движку
  // показать вердикт, а не открывает форму поверх живого/терминального
  // хода. setState в render-колбеке стабильной подписи -- ок (Object.
  // is на значении), движки рапортуют через useEffect.
  const [engineState, setEngineState] = useState<Record<string, string>>({});
  const engineOf = (act: string) => (status: string) =>
    setEngineState((m) => (m[act] === status ? m : { ...m, [act]: status }));
  // Приказы «показать вердикт» (#487): {act: токен}, движок срабатывает
  // по изменению своего токена
  const [reveal, setReveal] = useState<Record<string, Signal>>({});
  const revealOf = (act: string) => reveal[act] ?? null;
  if (session.role !== "admin") return null;
  return <Group gap={4} wrap="nowrap" role="group" aria-label={`действия ${row.name}`}
    style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
    <IconButton label="экран" variant="subtle" size="lg" onClick={() => setViewer("tail")}>
      <IconTerminal2 size={18} />
    </IconButton>
    {viewer && <PuppetCard session={session} target={row.name} onClose={() => setViewer(null)} />}
    <IconButton label="правка" variant="subtle" size="lg"
                onClick={() => { setReopen(null); setWin(true); }}>
      <IconSettings size={18} />
    </IconButton>
    {/* Движки -- в коже #520: без кнопок, вход -- приказ визарда. */}
    <ModelBox session={session} row={row} order={order} />
    <UpdateBox session={session} row={row} order={order} reveal={revealOf("update")}
               onState={engineOf("update")}
               onReopen={() => { setReopen("update"); setWin(true); }} />
    <RestartBox session={session} row={row} order={order}
                reveal={revealOf("restart")} onState={engineOf("restart")} />
    <DestructiveBox session={session} row={row} order={order}
                    reveal={revealOf("destructive")} onState={engineOf("destructive")} />
    <ControlWindow opened={win} onClose={() => { setWin(false); setReopen(null); }}
                   onOrder={(o) => setOrder({ at: Date.now(), act: o.act, params: o.params })}
                   row={row} session={session} initialEntry={reopen}
                   engineState={engineState}
                   onReveal={(act) => setReveal((m) =>
                     ({ ...m, [act]: { at: Date.now(), act, params: {} } }))} />
  </Group>;
}
