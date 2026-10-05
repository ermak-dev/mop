// Update из строки папета: fresh -- не «что-то» и не умолчание,
// а явный выбор из трёх режимов с последствиями на экране (слова #473):
//   продолжить как было  -> fresh=false: разговор живёт, ничего не гибнет;
//   свежий клон          -> fresh=true при своём origin: клон начисто,
//                           разговор гибнет;
//   другой origin        -> fresh=true + новый origin: пересадка, разговор
//                           гибнет; origin -- явное поле, пустое не постится.
// Выбор без режима невозможен: ни одна радио не отмечена заранее, POST без
// выбора не уходит. #513: keep обратим -- «продолжить» шлёт POST сразу,
// без моды подтверждения; replace и remove необратимы (гибнут клон и
// разговор) -- у них подтверждение точной цели (имя, узел, состояние)
// остаётся, как у разрушительных (#473). Ход операции -- общий
// useAdminOp; результат в памяти страницы, перезагрузка начинает заново.
import { useState } from "react";
import { IconSettings } from "@tabler/icons-react";
import { IconButton } from "./IconButton";
import { OperationResult } from "./OperationResult";
import { useSignal, type Signal } from "./ControlWindow";
import { Button, Code, Group, Modal, Radio, Stack, Text, TextInput } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { TERMINAL, useAdminOp } from "../useAdminOp";

import { opWord } from "./RestartBox";

/** Последствия режима: что гибнет, что живёт -- слова #473. */
const MODES: Record<string, { label: string; lines: string[] }> = {
  keep: {
    label: "продолжить как было",
    lines: [
      "разговор продолжится: контекст сессии остаётся",
      "несохранённая работа в клоне не трогается: ничего не гибнет",
    ],
  },
  replace: {
    label: "свежий клон, тот же origin",
    lines: [
      "клон будет пересобран начисто: несохранённая работа погибнет",
      "разговор погибнет: сессия начнётся с нуля",
    ],
  },
  remove: {
    label: "другой origin (пересадка)",
    lines: [
      "клон будет пересобран от нового origin: несохранённая работа погибнет",
      "разговор погибнет: сессия начнётся с нуля",
      "папет продолжит жить под тем же именем",
    ],
  },
};

export function UpdateBox({ session, row, icon = true, signal = null }: {
  session: Session;
  /** Строка ростера: подтверждение показывает её факты, origin -- hers. */
  row: Puppet;
  /** Кожа иконки в строке (#516): пункт окна шлёт сигнал без иконки. */
  icon?: boolean;
  /** Сигнал-токен пункта окна (#516): срабатывание по изменению. */
  signal?: Signal;
}) {
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  const [mode, setMode] = useState<keyof typeof MODES | null>(null);
  const [origin, setOrigin] = useState("");
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  if (session.role !== "admin") return null;
  // Вход (#516): пункт окна или иконка строки -- один и тот же ход:
  // живой ход показывает результат, свободный бокс открывает форму.
  const enter = () => { if (op.status !== "idle") setResult(true); else setOpen(true); };
  useSignal(signal, enter);
  const ready = mode !== null && (mode !== "remove" || origin.trim() !== "");
  const params = mode === "keep" ? { fresh: false }
    : mode === "replace" ? { fresh: true }
    : mode === "remove" && origin.trim() ? { fresh: true, origin: origin.trim() }
    : null;
  const go = () => {
    if (!params || live) return;
    setOpen(false); setConfirm(false); setMode(null); setOrigin(""); setResult(true);
    void submit("update", params);
  };
  // «продолжить»: keep обратим -- POST сразу (#513); fresh-режимы
  // необратимы -- сначала подтверждение точной цели
  const next = () => {
    if (!params || live) return;
    if (mode === "keep") { go(); return; }
    setConfirm(true);
  };
  return (
    <Group gap="sm">
      {/* «правка», не «обновить»: слово занято обновлением хвоста экрана */}
      {icon && (
        <IconButton label="правка" variant="subtle" size="lg" onClick={enter}>
          <IconSettings size={18} />
        </IconButton>
      )}
      <OperationResult opened={result} onClose={() => setResult(false)}
        onAgain={() => { setResult(false); setOpen(true); }} title="правка папета"
        target={row.name} op={op} word={opWord(op.status)} testId="update-verdict" />
      {open && !confirm && (
        <Modal opened onClose={() => setOpen(false)} size="md"
               title="правка папета" data-testid="update-form"
               closeButtonProps={{ "aria-label": "закрыть" }}>
          <Stack gap="sm" data-testid="update-modes">
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            <Radio.Group value={mode} onChange={(v) => setMode(v as keyof typeof MODES)}>
              <Stack gap={8} mt="xs">
                {(Object.keys(MODES) as (keyof typeof MODES)[]).map((k) => (
                  <Radio key={k} value={k} label={MODES[k].label} />
                ))}
              </Stack>
            </Radio.Group>
            {/* последствия выбранного -- до подтверждения, слова #473 */}
            {mode && MODES[mode].lines.map((l) => (
              <Text key={l} size="sm" c="dimmed">{l}</Text>
            ))}
            {mode === "remove" && (
              <TextInput label="новый origin" value={origin}
                         onChange={(e) => setOrigin(e.target.value)}
                         data-testid="update-origin" />
            )}
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => setOpen(false)}>отмена</Button>
              <Button disabled={!ready || live} onClick={next}>
                продолжить
              </Button>
            </Group>
          </Stack>
        </Modal>
      )}
      {/* Подтверждение -- точная цель: имя, узел, состояние с экрана (#465). */}
      {confirm && (
        <Modal opened onClose={() => { setConfirm(false); setOpen(false); }}
               size="md" title="подтвердите update"
               data-testid="update-confirm"
               closeButtonProps={{ "aria-label": "закрыть" }}>
          <Stack gap="sm">
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            <Text size="sm">узел: {row.node}</Text>
            <Text size="sm">состояние: <Code>{row.state}</Code></Text>
            {mode && MODES[mode].lines.map((l) => (
              <Text key={l} size="sm" c="dimmed">{l}</Text>
            ))}
            {mode === "remove" && (
              <Text size="sm">новый origin: <Code>{origin.trim()}</Code></Text>
            )}
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => { setConfirm(false); setOpen(false); }}>
                отмена
              </Button>
              <Button color="blue" disabled={!params || live} onClick={go}>
                подтвердить
              </Button>
            </Group>
          </Stack>
        </Modal>
      )}
    </Group>
  );
}
