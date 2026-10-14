import { Group, Modal, Text } from '@mantine/core';
import type { Session } from '../useSession';
import { HybridWorkspace } from './HybridWorkspace';
import { LABELS } from './labels';

export function PuppetCard({ session, target, role, onClose }: {
  session: Session;
  target: string | null;
  role?: string | null;
  onClose: () => void;
}) {
  if (!target || session.role !== 'admin') return null;
  return <Modal opened onClose={onClose} size="min(1000px, calc(100vw - 32px))" yOffset={16}
    title={<Group gap="sm"><Text fw={600}>экран {target}</Text>
      {role && <Text size="sm" c="dimmed">Role: {role}</Text>}</Group>}
    data-testid="puppet-card" padding="md" radius="md" closeButtonProps={{ 'aria-label': LABELS.close }}
    styles={{
      content: { height: 'calc(100dvh - 32px)', maxHeight: 'calc(100dvh - 32px)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden' },
      header: { flexShrink: 0 },
      body: { flex: 1, minHeight: 0, padding: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' },
    }}>
    <HybridWorkspace key={`${session.login}\0${session.role}\0${target}`} session={session} target={target} />
  </Modal>;
}
