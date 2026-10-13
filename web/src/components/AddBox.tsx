// Реестр формы -- useProjects; после подтверждённого 202 ход запуска
// принадлежит ростеру. Потерянный первый ответ не даёт основания закрыть
// форму или автоматически повторить add.
import { useState } from "react";
import { Button, Group, Modal, Radio, Stack, Text, TextInput } from "@mantine/core";
import type { Session } from "../useSession";
import { TERMINAL, useAdminOp } from "../useAdminOp";
import { useProjects } from "../useProjects";
import { opWord } from "./RestartBox";
import { LABELS } from "./labels";

export function AddBox({ session }: { session: Session }) {
  const { op, submit, forget } = useAdminOp(session, null);
  const { projects, loading, error: registryError, ask } = useProjects(session, false);
  const [open, setOpen] = useState(false);
  const [origin, setOrigin] = useState<string | null>(null);
  const [branch, setBranch] = useState("");
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  if (session.role !== "admin") return null;
  const picked = projects.find((p) => p.origin === origin) ?? null;
  const openForm = () => {
    if (live) return;
    if (op.status !== "unknown") forget();
    setOrigin(null);
    setBranch("");
    setOpen(true);
    ask();
  };
  const add = async () => {
    if (!picked || live) return;
    const params: Record<string, unknown> = { origin: picked.origin };
    if (branch.trim()) params.branch = branch.trim();
    const id = await submit("add", params);
    if (id) {
      setOpen(false);
      setOrigin(null);
      setBranch("");
    }
  };
  return (
    <Group gap="sm">
      <Button size="xs" variant="light" disabled={live} onClick={openForm}>
        завести папета
      </Button>
      {open && (
        <Modal opened onClose={() => setOpen(false)} size="md"
               title="завести папета" data-testid="add-form"
               closeButtonProps={{ "aria-label": LABELS.close }}>
          <Stack gap="sm">
            {registryError ? <Text c="red">{registryError}</Text> : null}
            {!registryError && loading ? (
              <Text c="dimmed" size="sm">реестр грузится</Text>
            ) : null}
            {!registryError && !loading && projects.length === 0 ? (
              <Text c="dimmed" size="sm">нет зарегистрированных проектов</Text>
            ) : null}
            <Radio.Group
              label="Проект"
              value={origin}
              onChange={setOrigin}
              data-testid="add-project"
            >
              <Stack gap={6} mt="xs">
                {projects.map((p) => (
                  <Radio key={p.origin} value={p.origin}
                         label={<Text size="sm">{p.name} <Text span size="xs"
                                   c="dimmed">{p.origin}</Text></Text>} />
                ))}
              </Stack>
            </Radio.Group>
            <TextInput
              label="Ветка (необязательна)"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              data-testid="add-branch"
            />
            <Text size="sm" c="dimmed">
              имя папета назначит сервер; после принятия команды
              запуск отображается в ростере
            </Text>
            {!op.id && TERMINAL.has(op.status) ? (
              <Text size="sm" c={op.status === "unknown" ? "yellow" : "red"}
                    data-testid="add-verdict">
                {opWord(op.status)}
                {op.lost ? " (первый ответ потерян; проверьте ростер перед повтором)" : ""}
              </Text>
            ) : null}
            {op.error ? <Text size="sm" c="red">{op.error}</Text> : null}
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => setOpen(false)}>{LABELS.cancel}</Button>
              <Button onClick={() => { void add(); }} disabled={!picked || live}
                      loading={op.status === "submitted"}
                      data-testid="add-submit">завести</Button>
            </Group>
          </Stack>
        </Modal>
      )}
    </Group>
  );
}
