import { IconX } from '@tabler/icons-react';
import type { Puppet } from '../types';
import type { Session } from '../useSession';
import { HybridWorkspace } from './HybridWorkspace';
import { PuppetActions } from './PuppetActions';
import { IconButton } from './IconButton';
import { LABELS } from './labels';

export function PuppetCard({ session, target, role, rows = [], onClose }: {
  session: Session;
  target: string | null;
  role?: string | null;
  rows?: Puppet[];
  onClose: () => void;
}) {
  if (session.role !== 'admin') return null;
  return <aside className="dashboard-screen" hidden={!target} aria-label={`экран ${target ?? ''}`}
    data-testid={target ? 'puppet-card' : undefined}>
    <HybridWorkspace session={session} target={target} role={role}
      actions={rows.map(row => <div key={`${session.login}\0${row.name}`} className="dashboard-screen-actions" hidden={row.name !== target}>
        <PuppetActions row={row} session={session} active={row.name === target} />
      </div>)}
      close={<IconButton label={LABELS.close} variant="subtle" size="lg" onClick={onClose}><IconX size={18} /></IconButton>} />
  </aside>;
}
