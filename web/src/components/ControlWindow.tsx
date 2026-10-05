// Окно управления целью (#516): клик «правка» в строке открывает перечень
// всех операций цели пунктами -- правка (update), модель, рестарт и три
// разрушительных (recycle/delete/wipe). Строка после волны #507-#513
// несла восемь иконок; минимализм оператора: в строке «экран» и «правка»,
// остальное -- здесь. Окно -- только навигация: выбор пункта закрывает
// его и запускает поток бокса сигнал-токеном ({at}); боксы остаются
// смонтированными в строке со своей логикой (useAdminOp-стейт переживает
// окно -- вердикт, пришедший после закрытия, не теряется), точка входа
// меняет кожу (icon=false: без иконки, вход по сигналу). Вход в каждую
// операцию не длиннее прежнего: клик «правка» -> пункт -> действие.
import { useEffect, useRef } from "react";
import { Button, Modal, Stack, Text, Tooltip } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { restartReason } from "./RestartBox";

/** Какой поток запустить выбором пункта (#516). */
export type ControlEntry = "update" | "model" | "restart" | "recycle" | "delete" | "wipe";

/** Сигнал-токен пункта: срабатывание -- по изменению, mount молчит. */
export type Signal = { at: number } | null;

/** Сигнал сработал (#516): срабатывание -- по изменению токена; первый
 * рендер (mount) молчит -- иначе монтаж бокса стартовал бы поток. fire
 * держится через latest-ref: замыкание видит свежий стейт бокса, а не
 * первый рендер. */
export function useSignal(signal: Signal, fire: () => void) {
  const seen = useRef(signal);
  const latest = useRef(fire);
  latest.current = fire;
  useEffect(() => {
    if (signal && signal !== seen.current) {
      seen.current = signal;
      latest.current();
    }
  }, [signal]);
}

export function ControlWindow({ opened, onClose, onPick, row, session }: {
  opened: boolean;
  onClose: () => void;
  /** Выбранный пункт: закрывает окно и запускает поток бокса. */
  onPick: (entry: ControlEntry) => void;
  row: Puppet;
  session: Session;
}) {
  if (!opened) return null;
  // Дизейбл-причина рестарта -- признак строки (#508), виден до клика
  const blocked = session.role === "admin" ? restartReason(row) : null;
  return (
    <Modal opened onClose={onClose} size="md" title="правка папета"
           data-testid="control-window"
           closeButtonProps={{ "aria-label": "закрыть" }}>
      <Stack gap="xs">
        {/* цель -- в самом окне: пункты ниже относятся к ней */}
        <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
        <Button variant="light" onClick={() => onPick("update")}>правка</Button>
        <Button variant="light" onClick={() => onPick("model")}>модель</Button>
        <Tooltip label={blocked ?? ""} disabled={!blocked}>
          <Button variant="light" color="orange" disabled={!!blocked}
                  aria-label={blocked ?? "рестарт"}
                  onClick={() => onPick("restart")}>рестарт</Button>
        </Tooltip>
        {/* разрушительные -- своим красным, поток #512 байт в байт */}
        <Button variant="light" color="red" onClick={() => onPick("recycle")}>recycle</Button>
        <Button variant="light" color="red" onClick={() => onPick("delete")}>delete</Button>
        <Button variant="light" color="red" onClick={() => onPick("wipe")}>wipe</Button>
      </Stack>
    </Modal>
  );
}
