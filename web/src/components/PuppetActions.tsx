import { useState } from "react";
import { Group, Modal, Stack, Text } from "@mantine/core";
import { IconMessage } from "@tabler/icons-react";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { IconButton } from "./IconButton";
import { ModelBox } from "./ModelBox";
import { RestartBox } from "./RestartBox";
import { SendForm } from "./SendForm";
import { UpdateBox } from "./UpdateBox";

export function PuppetActions({ row, session }: { row: Puppet; session: Session }) {
  const [send, setSend] = useState(false);
  if (session.role !== "admin") return null;
  return <Group gap={4} wrap="nowrap" role="group" aria-label={`действия ${row.name}`}>
    <IconButton label="сообщение" variant="subtle" size="lg" onClick={() => setSend(true)}>
      <IconMessage size={18} />
    </IconButton>
    <ModelBox session={session} row={row} />
    <UpdateBox session={session} row={row} />
    <RestartBox session={session} row={row} />
    {send && <Modal opened onClose={() => setSend(false)} size="lg" title="сообщение папету"
      closeButtonProps={{ "aria-label": "закрыть" }}>
      <Stack gap="md">
        <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
        <SendForm session={session} target={row.name} />
      </Stack>
    </Modal>}
  </Group>;
}
