// Смена модели из строки папета: выбор из каталога прокси --
// единственный источник (useModels, GET /api/admin/models), своего списка
// страница не держит. Подтверждение -- точная цель (имя, узел, модель),
// как у рестарта (#465) и правки (#472). Ход операции -- общий useAdminOp:
// succeeded приезжает только после телеметрии сервера (та же сессия,
// свежая запись, та же модель) -- «диалог закрылся» подтверждением не
// считается. Радио, а не Select: выпадашка не открывается в jsdom (#472).
// Не-admin кнопки не видит; результат в памяти страницы.
import { useState } from "react";
import { IconCpu } from "@tabler/icons-react";
import { IconButton } from "./IconButton";
import { OperationResult } from "./OperationResult";
import { Button, Code, Group, Modal, Radio, Stack, Text } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useModels } from "../useModels";
import { TERMINAL, useAdminOp } from "../useAdminOp";

import { opWord } from "./RestartBox";

export function ModelBox({ session, row }: {
  session: Session;
  /** Строка ростера: подтверждение показывает её факты. */
  row: Puppet;
}) {
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  const [model, setModel] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  // Каталог -- при открытии формы (want=open): чтение без действий.
  const { models, error: catalogError } = useModels(
    session, session.role === "admin" && open);
  if (session.role !== "admin") return null;
  const go = () => {
    if (!model || live) return;
    setOpen(false); setConfirm(false); setModel(null); setResult(true);
    void submit("model", { model });
  };
  return (
    <Group gap="sm">
      <IconButton label="модель" variant="subtle" size="lg"
              onClick={() => { if (op.status !== "idle") setResult(true); else { setModel(null); setOpen(true); } }}>
        <IconCpu size={18} />
      </IconButton>
      <OperationResult opened={result} onClose={() => setResult(false)}
        onAgain={() => { setResult(false); setModel(null); setOpen(true); }} title="смена модели"
        target={row.name} op={op} word={opWord(op.status)} testId="model-verdict" />
      {open && !confirm && (
        <Modal opened onClose={() => setOpen(false)} size="md"
               title="смена модели" data-testid="model-form"
               closeButtonProps={{ "aria-label": "закрыть" }}>
          <Stack gap="sm" data-testid="model-choices">
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            {catalogError ? <Text size="sm" c="red">{catalogError}</Text> : null}
            {models.length ? (
              <Radio.Group value={model}
                           onChange={(v) => setModel(v)}>
                <Stack gap={8} mt="xs">
                  {models.map((m) => (
                    <Radio key={m} value={m} label={m} />
                  ))}
                </Stack>
              </Radio.Group>
            ) : !catalogError ? (
              <Group gap="sm" mt="xs"><Text c="dimmed" size="sm">каталог грузится</Text></Group>
            ) : null}
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => setOpen(false)}>отмена</Button>
              <Button disabled={!model || live} onClick={() => setConfirm(true)}>
                продолжить
              </Button>
            </Group>
          </Stack>
        </Modal>
      )}
      {/* Подтверждение -- точная цель: имя, узел, заказанная модель. */}
      {confirm && model && (
        <Modal opened onClose={() => { setConfirm(false); setOpen(false); }}
               size="md" title="подтвердите смену модели"
               data-testid="model-confirm"
               closeButtonProps={{ "aria-label": "закрыть" }}>
          <Stack gap="sm">
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            <Text size="sm">узел: {row.node}</Text>
            <Text size="sm">модель: <Code>{model}</Code></Text>
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => { setConfirm(false); setOpen(false); }}>
                отмена
              </Button>
              <Button color="blue" disabled={!model || live} onClick={go}>
                подтвердить
              </Button>
            </Group>
          </Stack>
        </Modal>
      )}
    </Group>
  );
}
