// Смена модели из строки папета: выбор из каталога прокси --
// единственный источник (useModels, GET /api/admin/models), своего списка
// страница не держит. #513: смена модели обратима -- «продолжить» шлёт
// POST сразу, мода подтверждения ушла (подтверждение осталось у
// необратимых). Ход операции -- общий useAdminOp: succeeded приезжает
// только после телеметрии сервера (та же сессия, свежая запись, та же
// модель) -- «диалог закрылся» подтверждением не считается. Радио, а не
// Select: выпадашка не открывается в jsdom (#472 опыт).
// Не-admin кнопки не видит; результат в памяти страницы.
import { useState } from "react";
import { useOperationNotice } from "../useOperationNotice";
import { IconCpu } from "@tabler/icons-react";
import { IconButton } from "./IconButton";
import { Button, Group, Modal, Radio, Stack, Text } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useModels } from "../useModels";
import { TERMINAL, useAdminOp } from "../useAdminOp";

import { opWord } from "./RestartBox";

export function ModelBox({ session, row }: {
  session: Session;
  /** Строка ростера: цель смены модели. */
  row: Puppet;
}) {
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  const [model, setModel] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  // Каталог -- при открытии формы (want=open): чтение без действий.
  const { models, error: catalogError } = useModels(
    session, session.role === "admin" && open);
  useOperationNotice(op, {
    scope: `${session.login}\0${session.role}\0${row.name}`,
    title: `Смена модели: ${row.name}`,
    word: op.status === "succeeded" ? "модель изменена" : opWord(op.status),
    enabled: session.role === "admin",
    onSettled: () => { void fetch("/api/pool", { credentials: "same-origin" }).catch(() => {}); },
  });
  if (session.role !== "admin") return null;
  const current = row.llm && row.llm !== "-" ? row.llm : null;
  const go = () => {
    if (!model || live) return;
    setOpen(false); setModel(null);
    void submit("model", { model });
  };
  return (
    <Group gap="sm">
      <IconButton label="модель" variant="subtle" size="lg"
              onClick={() => { setModel(null); setOpen(true); }}>
        <IconCpu size={18} />
      </IconButton>
      {open && (
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
                    <Radio key={m} value={m}
                           label={m === current ? `${m} — текущая` : m}
                           disabled={m === current} />
                  ))}
                </Stack>
              </Radio.Group>
            ) : !catalogError ? (
              <Group gap="sm" mt="xs"><Text c="dimmed" size="sm">каталог грузится</Text></Group>
            ) : null}
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => setOpen(false)}>отмена</Button>
              <Button disabled={!model || live} onClick={go}>
                продолжить
              </Button>
            </Group>
          </Stack>
        </Modal>
      )}
    </Group>
  );
}
