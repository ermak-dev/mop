// Конечный operation audit #626/#627, не полный журнал событий шины.
// Selection -- только ID + source; подтверждённые DTO остаются у общего owner.
import { useEffect, useState } from 'react';
import { Button, Group, Table, Text, TextInput, UnstyledButton } from '@mantine/core';
import { auditQuery } from '../audit';
import type { AuditItem, AuditQuery } from '../audit';
import { fullAt } from '../journalView';
import { EmptyNote, SectionTitle } from './Section';
import { JournalModal } from './JournalModal';
export const EMPTY = 'No operation audit evidence in the current scope';
export function JournalPage({ journal, error = null, sourceKey, loading = false, nextCursor = null, more, refresh, query = {}, onQuery }: {
  journal: readonly AuditItem[];
  error?: string | null;
  sourceKey: string;
  loading?: boolean;
  nextCursor?: string | null;
  more?: () => void;
  refresh?: () => void;
  query?: AuditQuery;
  onQuery?: (query: AuditQuery) => void;
}) {
  const [projectDraft, setProjectDraft] = useState(query.projectId ?? '');
  const [limitDraft, setLimitDraft] = useState(String(query.limit ?? 100));
  const [draftError, setDraftError] = useState<string | null>(null);
  const [selection, setSelection] = useState<{ id: string; source: string } | null>(null);
  const denied = error !== null;
  const held = denied || selection?.source !== sourceKey ? null : journal.find(row => row.id === selection?.id);
  useEffect(() => { setSelection(null); }, [denied, sourceKey]);
  const shown = denied ? [] : journal;
  const open = (row: AuditItem) => setSelection({ id: row.id, source: sourceKey });
  return (
    <div data-testid="journal-page" aria-busy={loading}>
      <SectionTitle title="Operation audit" />
      <Text size="sm" c="dimmed" mb="sm">Finite operation evidence, newest first; not all bus events, idle notifications or private message contents.</Text>
      {error && <Text role="alert" c="red" size="sm" mb="xs">{error}</Text>}
      {onQuery && <form onSubmit={event => {
        event.preventDefault();
        try {
          if (!/^[1-9][0-9]*$/.test(limitDraft)) throw new Error('Page size must be a decimal integer within 1–100');
          const selected = { projectId: projectDraft.trim() || undefined, limit: Number(limitDraft) };
          auditQuery(selected); setDraftError(null); onQuery(selected);
        } catch (error) { setDraftError(error instanceof Error ? error.message : 'Invalid audit scope'); }
      }}>
        <Group mb="sm" align="end">
          <TextInput label="Project id (optional)" value={projectDraft} onChange={event => setProjectDraft(event.currentTarget.value)} />
          <TextInput label="Page size (1–100)" type="number" min={1} max={100} step={1} value={limitDraft} onChange={event => setLimitDraft(event.currentTarget.value)} />
          <Button type="submit" size="xs" disabled={loading}>Apply audit scope</Button>
        </Group>
        {draftError && <Text role="alert" c="red" size="sm">{draftError}</Text>}
      </form>}
      <Group mb="sm">
        {refresh && <Button size="xs" variant="light" disabled={loading} onClick={refresh}>Refresh operation audit</Button>}
        {loading && <Text role="status" size="sm">Loading operation audit…</Text>}
      </Group>
      {shown.length ? (
        <Table.ScrollContainer minWidth={760}>
          <Table striped highlightOnHover withTableBorder>
            <Table.Thead><Table.Tr>
              {['Time', 'Action', 'State', 'Code', 'Target kind / id', 'Node id', 'Project ids', 'Reason'].map(label => <Table.Th key={label}>{label}</Table.Th>)}
            </Table.Tr></Table.Thead>
            <Table.Tbody>{shown.map(row => (
              <Table.Tr key={row.id} onClick={() => open(row)}>
                <Table.Td><UnstyledButton aria-label={`Open audit evidence ${row.id}`} onClick={event => { event.stopPropagation(); open(row); }}>
                  {fullAt(Date.parse(row.created_at) / 1000)}
                </UnstyledButton></Table.Td>
                <Table.Td>{row.action}</Table.Td><Table.Td>{row.state}</Table.Td><Table.Td>{row.code || '—'}</Table.Td>
                <Table.Td>{row.target.kind} / {row.target.id}</Table.Td><Table.Td>{row.node_id}</Table.Td>
                <Table.Td>{row.project_ids.join(', ')}</Table.Td><Table.Td><Text lineClamp={2}>{row.reason || '—'}</Text></Table.Td>
              </Table.Tr>
            ))}</Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      ) : !error && !loading && <EmptyNote>{EMPTY}</EmptyNote>}
      {!error && nextCursor && more && <Button mt="sm" disabled={loading} onClick={more}>Load older evidence</Button>}
      {held && <JournalModal entry={held} onClose={() => setSelection(null)} />}
    </div>
  );
}
