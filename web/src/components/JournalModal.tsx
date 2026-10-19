// Только frozen audit metadata: никаких full_entry, private params или bus phrases.
import { Box, Button, Code, Modal, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { auditWire } from '../audit';
import type { AuditItem } from '../audit';
export function JournalModal({ entry, onClose }: { entry: AuditItem; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const evidence = auditWire(entry), title = `Operation audit evidence ${evidence.id}`;
  const fields: [string, string][] = [
    ['Evidence id', evidence.id], ['Operation id', evidence.operation_id], ['Operation revision', evidence.revision],
    ['Created at (UTC)', evidence.created_at], ['Action', evidence.action], ['State', evidence.state],
    ['Code', evidence.code], ['Reason', evidence.reason], ['Target kind', evidence.target.kind],
    ['Target id', evidence.target.id], ['Target incarnation', evidence.target.incarnation ?? '—'],
    ['Node id', evidence.node_id], ['Project ids', evidence.project_ids.join(', ')],
  ];
  const copy = async () => {
    try { await navigator.clipboard.writeText(JSON.stringify(evidence, null, 2)); setCopied(true); setCopyError(false); }
    catch { setCopied(false); setCopyError(true); }
  };
  return (
    <Modal opened onClose={onClose} title={title} attributes={{ content: { 'aria-label': title } }} closeButtonProps={{ 'aria-label': 'Close audit evidence' }} centered size="lg">
      <Stack gap={6}>
        {fields.map(([label, value]) => <Text key={label} size="sm" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
          <Text span c="dimmed" mr={8}>{label}:</Text><Box component="span">{value || '—'}</Box>
        </Text>)}
        <Code block>{JSON.stringify(evidence, null, 2)}</Code>
        <Button size="xs" variant="light" aria-label="Copy audit evidence" onClick={copy}>{copied ? 'Copied' : 'Copy audit evidence'}</Button>
        {copyError && <Text role="alert" size="sm">Clipboard unavailable</Text>}
      </Stack>
    </Modal>
  );
}
