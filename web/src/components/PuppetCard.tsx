import { Modal } from '@mantine/core';
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
  return <Modal opened onClose={onClose} size="calc(100vw - 32px)" xOffset={16} yOffset={16}
    title={`экран ${target}`}
    data-testid="puppet-card" padding="md" radius="md" closeButtonProps={{ 'aria-label': LABELS.close }}
    styles={{
      content: { height: 'calc(100dvh - 32px)', maxHeight: 'calc(100dvh - 32px)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden' },
      header: { flexShrink: 0 },
      body: { flex: 1, minHeight: 0, padding: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' },
    }}>
    <HybridWorkspace key={`${session.login}\0${session.role}\0${target}`} session={session} target={target} role={role} />
  </Modal>;
}
