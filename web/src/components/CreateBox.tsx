// Create (#627): черновик формы локален, Operation и recovery -- общий store.
// ID одобренного определения вводится явно: списка определений API пока нет,
// угадывать роль по имени/проекту или брать старый /api/admin нельзя.
import { useState } from 'react';
import { Alert, Button, Group, Modal, Stack, Text, TextInput } from '@mantine/core';
import { poolNetwork } from '../poolNetwork';
import { useAdminOp } from '../useAdminOp';
import { hasControls, type Session } from '../useSession';
import { opWord } from './RestartBox';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const LOCKED = new Set(['submitted', 'accepted', 'running', 'reconciling', 'unknown', 'partial']);
export function CreateBox({ session }: { session: Session }) {
  const [opened, setOpened] = useState(false);
  const [project, setProject] = useState('');
  const [node, setNode] = useState('');
  const [definition, setDefinition] = useState('');
  const { op, submit } = useAdminOp(session, project);
  if (!hasControls(session)) return null;
  const locked = LOCKED.has(op.status);
  const valid = [project, node, definition].every(value => UUID.test(value));
  return <>
    <Button onClick={() => setOpened(true)}>Завести папета</Button>
    <Modal opened={opened} onClose={() => setOpened(false)} title="Новый папет" centered>
      <Stack>
        <Text size="sm">Сервер назначит имя и инкарнацию. Нужны узел проекта, одобренное определение,
          одобренный bootstrap и готовый образ на узле.</Text>
        <TextInput label="ID проекта" value={project} disabled={locked}
          onChange={event => setProject(event.currentTarget.value.trim())} />
        <TextInput label="ID узла" value={node} disabled={locked}
          onChange={event => setNode(event.currentTarget.value.trim())} />
        <TextInput label="ID одобренного определения" value={definition} disabled={locked}
          onChange={event => setDefinition(event.currentTarget.value.trim())} />
        {op.status !== 'idle' && <Alert color={op.status === 'succeeded' ? 'teal'
          : op.status === 'unknown' || op.status === 'partial' ? 'yellow' : 'blue'}>
          {opWord(op.status)}{op.error ? `: ${op.error}` : ''}
          {op.id && <Text size="xs">Operation: {op.id}</Text>}
        </Alert>}
        {locked && <Text size="sm">Запрос уже отправлен. Не создавайте второй для выяснения исхода.</Text>}
        <Group justify="flex-end">
          {locked && <Button variant="default" onClick={() => { void poolNetwork.recover(); }}>Проверить исход</Button>}
          <Button variant="default" onClick={() => setOpened(false)}>Закрыть</Button>
          <Button disabled={!valid || locked} onClick={() => {
            void submit('create', { node_id: node, definition_id: definition });
          }}>Создать</Button>
        </Group>
      </Stack>
    </Modal>
  </>;
}
