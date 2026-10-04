// Update из строки папета: fresh -- не «что-то» и не умолчание,
// а явный выбор из трёх режимов с последствиями на экране (слова #473):
//   продолжить как было  -> fresh=false: разговор живёт, ничего не гибнет;
//   свежий клон          -> fresh=true при своём origin: клон начисто,
//                           разговор гибнет;
//   другой origin        -> fresh=true + новый origin: пересадка, разговор
//                           гибнет; origin -- явное поле, пустое не постится.
// Выбор без режима невозможен: ни одна радио не отмечена заранее, POST без
// выбора не уходит. Подтверждение -- точная цель (имя, узел, состояние),
// как у рестарта (#465) и разрушительных (#473). Ход операции -- общий
// useAdminOp; результат в памяти страницы, перезагрузка начинает заново.
// Регистрация update у исполнителя приедет из #470: до тех сервер
// ответит unsupported, фраза показывается как есть.
import { useState } from "react";
import { IconSettings } from "@tabler/icons-react";
import { IconButton } from "./IconButton";
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

export function UpdateBox({ session, row }: {
  session: Session;
  /** Строка ростера: подтверждение показывает её факты, origin -- hers. */
  row: Puppet;
}) {
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  const [mode, setMode] = useState<keyof typeof MODES | null>(null);
  const [origin, setOrigin] = useState("");
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  if (session.role !== "admin") return null;
  const ready = mode !== null && (mode !== "remove" || origin.trim() !== "");
  const params = mode === "keep" ? { fresh: false }
    : mode === "replace" ? { fresh: true }
    : mode === "remove" && origin.trim() ? { fresh: true, origin: origin.trim() }
    : null;
  const go = () => {
    if (!params || live) return;
    setOpen(false); setConfirm(false); setMode(null); setOrigin("");
    void submit("update", params);
  };
  return (
    <Group gap="sm">
      {/* «правка», не «обновить»: слово занято обновлением хвоста экрана */}
      <IconButton label="правка" variant="subtle" size="lg" disabled={live}
              onClick={() => setOpen(true)}>
        <IconSettings size={18} />
      </IconButton>
      {op.status !== "idle" && (
        <Text size="sm" c={op.status === "succeeded" ? "teal"
                            : op.status === "unknown" ? "yellow" : "red"}
              data-testid="update-verdict">
          {opWord(op.status)}
          {op.lost ? " (первый ответ потерян)" : ""}
        </Text>
      )}
      {op.error ? <Text size="sm" c="red">{op.error}</Text> : null}
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
              <Button disabled={!ready || live} onClick={() => setConfirm(true)}>
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
