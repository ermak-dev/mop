import { Button, Group, Loader, Modal, Stack, Text } from "@mantine/core";
import { TERMINAL, type AdminOpState } from "../useAdminOp";
import { LABELS } from "./labels";

export function OperationResult({ opened, onClose, onAgain, title, target, op, word, testId }: {
  opened: boolean; onClose: () => void; onAgain: () => void; title: string; target: string;
  op: AdminOpState; word: string; testId: string;
}) {
  if (!opened) return null;
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  return <Modal opened onClose={onClose} title={`${title}: ${target}`} size="md"
    closeButtonProps={{ "aria-label": LABELS.close }}>
    <Stack gap="sm">
      <Group gap="sm">
        {live && <Loader size="sm" />}
        <Text size="sm" data-testid={testId} c={live ? "dimmed"
          : op.status === "succeeded" ? "teal" : op.status === "unknown" ? "yellow" : "red"}>
          {word}{op.error ? `: ${op.error}` : ""}{op.lost ? " (первый ответ потерян)" : ""}
        </Text>
      </Group>
      {!live && op.status !== "idle" && <Group justify="flex-end">
        <Button variant="light" onClick={onAgain}>{LABELS.again}</Button>
      </Group>}
    </Stack>
  </Modal>;
}
