// Действия строки (#516/#522): в строке -- «экран» и «правка»; все
// операции цели -- визардом ControlWindow, ЕДИНСТВЕННЫМ диалогом:
// финальный экран «что произойдёт» у каждой операции, «Завершить» =
// согласие, экран хода -- тоже экран визарда. Движки-«боксы»
// (useAdminOp) остаются смонтированными здесь же без кожи: их стейт
// переживает окно, вердикт, пришедший после закрытия, не теряется;
// визард шлёт им один приказ-токен на строку, каждый движок берёт
// свой act и поднимает стейт наверх (engineState {status, error,
// lost}) -- по нему визард открывает экран хода при повторном входе
// (#487) без вторых POST. «новая команда» на экране хода возвращает
// визард к выбору.
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
import { StopBox } from "./StopBox";
import { UpdateBox } from "./UpdateBox";
import { DestructiveBox } from "./DestructiveBox";
import { ControlWindow, type ControlEntry, type Signal, type EngineState } from "./ControlWindow";
import { LABELS } from "./labels";

export function PuppetActions({ row, role, session }: { row: Puppet; role: string | null; session: Session }) {
  // экран или ничего (#507): второй источник -- stderr -- ушёл с кнопкой «лог»
  const [viewer, setViewer] = useState<"tail" | null>(null);
  // окно правки (#516/#522): визард операций цели
  const [win, setWin] = useState(false);
  // Возврат «новая команда» (#520): визард открывается сразу на шаге
  // обновления -- повтор обновления это новый выбор режима
  const [reopen, setReopen] = useState<ControlEntry | null>(null);
  // Приказ движкам (#520): один токен на строку, каждый берёт свой act
  const [order, setOrder] = useState<Signal>(null);
  // «Новая команда» (#522): футер хода просит движок показанного экрана
  // забыть вердикт -- иначе pick того же пункта diverted бы на старый
  // терминал навсегда. Токен один на строку, движки слушают свой ключ.
  const [forget, setForget] = useState<Signal>(null);
  // Стейт операций по движку (#487/#522): {engine: {status, error,
  // lost}}. Визард по нему открывает экран хода при повторном входе --
  // divert/reveal-токены ушли. setState в render-колбеке стабильной
  // подписи -- ок (Object.is на значении), движки рапортуют через
  // useEffect.
  const [engineState, setEngineState] = useState<Record<string, EngineState>>({});
  const engineOf = (act: string) => (op: EngineState) =>
    setEngineState((m) => (m[act]?.status === op.status
      && m[act]?.error === op.error && m[act]?.lost === op.lost ? m : { ...m, [act]: op }));
  if (session.role !== "admin") return null;
  return <Group gap={4} wrap="nowrap" role="group" aria-label={`действия ${row.name}`}
    style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
    <IconButton label={LABELS.screen} variant="subtle" size="lg" onClick={() => setViewer("tail")}>
      <IconTerminal2 size={18} />
    </IconButton>
    {viewer && <PuppetCard session={session} target={row.name} role={role} onClose={() => setViewer(null)} />}
    <IconButton label={LABELS.edit} variant="subtle" size="lg"
                onClick={() => { setReopen(null); setWin(true); }}>
      <IconSettings size={18} />
    </IconButton>
    {/* Движки -- в коже #522: без кнопок и без мод, вход -- приказ
        визарда, стейт поднимается наверх; forget -- «новая команда». */}
    <ModelBox session={session} row={row} order={order} />
    <UpdateBox session={session} row={row} order={order} forget={forget}
               onState={engineOf("update")} />
    <RestartBox session={session} row={row} order={order} forget={forget}
                onState={engineOf("restart")} />
    <StopBox session={session} row={row} order={order} forget={forget}
             onState={engineOf("stop")} />
    <DestructiveBox session={session} row={row} order={order} forget={forget}
                    onState={engineOf("destructive")} />
    <ControlWindow opened={win} onClose={() => { setWin(false); setReopen(null); }}
                   onOrder={(o) => setOrder({ at: Date.now(), act: o.act, params: o.params })}
                   onForget={() => setForget({ at: Date.now(), act: "forget", params: {} })}
                   row={row} session={session} initialEntry={reopen}
                   engineState={engineState} />
  </Group>;
}
