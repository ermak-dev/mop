// Форма add (#472): источник проекта -- реестр сервера (useProjects):
// имя и origin приходят с сервера, страница имя из origin не выводит
// второй раз. Выбор -- радио-группа (не Combobox): вариантов -- заведённые
// проекты, их немного, и выбор с видимым origin честнее выпадашки.
// Ветка -- опциональна; имя папета -- не поле формы: его говорит сервер
// в исходе операции. Ход -- общий useAdminOp: POST
// {action: "add", params: {origin, branch?}} без target -> 202 -> id ->
// вердикт из памяти страницы; перезагрузка начинает заново (id хранить
// негде, storage запрещён #463) -- ограничение названо в отчёте.
// Регистрация add в исполнителе приедет из #470: до тех пор сервер
// ответит unsupported, и фраза отказа будет показана как есть.
import { useState } from "react";
import { Button, Group, Modal, Radio, Stack, Text, TextInput } from "@mantine/core";
import type { Session } from "../useSession";
import { useAdminOp } from "../useAdminOp";
import { useProjects } from "../useProjects";
import { opWord } from "./RestartBox";

/** Слово исхода add: свой глагол у «succeeded», остальное -- общий словарь. */
export const addWord = (status: string): string =>
  status === "succeeded" ? "заведён" : opWord(status);

const TERMINAL = new Set(["succeeded", "refused", "failed", "partial", "unknown"]);

export function AddBox({ session }: { session: Session }) {
  const { op, submit } = useAdminOp(session, null);
  const { projects, error: registryError, ask } = useProjects(session, false);
  const [open, setOpen] = useState(false);
  const [origin, setOrigin] = useState<string | null>(null);
  const [branch, setBranch] = useState("");
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  if (session.role !== "admin") return null;
  const picked = projects.find((p) => p.origin === origin) ?? null;
  const add = () => {
    if (!picked || live) return;    // без проекта не постится, живой ход один
    const params: Record<string, unknown> = { origin: picked.origin };
    if (branch.trim()) params.branch = branch.trim();
    void submit("add", params);
  };
  return (
    <Group gap="sm">
      <Button size="xs" variant="light" disabled={live}
              onClick={() => { setOpen(true); ask(); }}>
        завести папета
      </Button>
      {op.status !== "idle" && (
        <Text size="sm" c={op.status === "succeeded" ? "teal"
                            : op.status === "unknown" ? "yellow" : "red"}
              data-testid="add-verdict">
          {addWord(op.status)}
          {op.lost ? " (первый ответ потерян)" : ""}
        </Text>
      )}
      {op.error ? <Text size="sm" c="red">{op.error}</Text> : null}
      {open && (
        <Modal opened onClose={() => setOpen(false)} size="md"
               title="завести папета" data-testid="add-form"
               closeButtonProps={{ "aria-label": "закрыть" }}>
          <Stack gap="sm">
            {registryError ? <Text c="red">{registryError}</Text> : null}
            {!registryError && projects.length === 0 ? (
              <Text c="dimmed" size="sm">реестр грузится</Text>
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
              имя папета назначит сервер; исход операции читается из памяти
              страницы -- после перезагрузки он начинается заново
            </Text>
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => setOpen(false)}>отмена</Button>
              <Button onClick={add} disabled={!picked || live}
                      loading={op.status === "submitted"}
                      data-testid="add-submit">завести</Button>
            </Group>
          </Stack>
        </Modal>
      )}
    </Group>
  );
}
