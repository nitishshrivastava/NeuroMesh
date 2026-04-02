/**
 * CheckpointViewer.tsx — FlowOS Checkpoint History Panel
 *
 * Displays the checkpoint history for the selected workflow or task.
 * Shows:
 *   - Checkpoint list with type, status, sequence, and git info
 *   - Checkpoint detail view with file changes
 *   - Revert action for committed/verified checkpoints
 *   - Progress indicator and metadata
 *
 * Data is fetched from GET /checkpoints?workflow_id=... and
 * GET /checkpoints/{id}/files
 */

import React, { useState, useEffect, useCallback, memo } from 'react'
import { format, formatDistanceToNow } from 'date-fns'
import {
  GitCommit,
  GitBranch,
  RefreshCw,
  ChevronRight,
  ChevronDown,
  RotateCcw,
  CheckCircle2,
  AlertCircle,
  Loader2,
  FileText,
  FilePlus,
  FileMinus,
  FileEdit,
  Tag,
  X,
} from 'lucide-react'
import { useWorkflowStore } from '../../store/workflowStore'
import { apiClient, normaliseError } from '../../api/client'

// ─── Types ────────────────────────────────────────────────────────────────────

interface CheckpointFile {
  file_id: string
  checkpoint_id: string
  path: string
  status: 'added' | 'modified' | 'deleted' | 'renamed' | string
  old_path: string | null
  size_bytes: number | null
  checksum: string | null
}

interface Checkpoint {
  checkpoint_id: string
  task_id: string
  workflow_id: string
  agent_id: string
  workspace_id: string
  checkpoint_type: string
  status: string
  sequence: number
  git_commit_sha: string | null
  git_branch: string
  git_tag: string | null
  message: string
  file_count: number
  lines_added: number
  lines_removed: number
  s3_archive_key: string | null
  task_progress: number | null
  notes: string | null
  checkpoint_metadata: Record<string, unknown>
  created_at: string
  verified_at: string | null
  reverted_at: string | null
}

interface CheckpointListResponse {
  items: Checkpoint[]
  total: number
  limit: number
  offset: number
}

// ─── API helpers ──────────────────────────────────────────────────────────────

async function fetchCheckpoints(workflowId: string): Promise<Checkpoint[]> {
  const res = await apiClient.get<CheckpointListResponse>('/checkpoints', {
    params: { workflow_id: workflowId, limit: 100, offset: 0 },
  })
  return res.data.items
}

async function fetchCheckpointFiles(
  checkpointId: string
): Promise<CheckpointFile[]> {
  const res = await apiClient.get<CheckpointFile[]>(
    `/checkpoints/${checkpointId}/files`
  )
  return res.data
}

async function revertCheckpoint(
  checkpointId: string,
  agentId: string
): Promise<Checkpoint> {
  const res = await apiClient.post<Checkpoint>(
    `/checkpoints/${checkpointId}/revert`,
    { agent_id: agentId, reason: 'Manual revert from UI' }
  )
  return res.data
}

// ─── Colour helpers ───────────────────────────────────────────────────────────

const CHECKPOINT_TYPE_COLORS: Record<string, string> = {
  manual: '#6366f1',
  auto: '#22c55e',
  pre_handoff: '#f59e0b',
  post_handoff: '#3b82f6',
  milestone: '#a855f7',
  recovery: '#ef4444',
}

const STATUS_COLORS: Record<string, string> = {
  pending: '#4b5563',
  committed: '#16a34a',
  verified: '#6366f1',
  reverted: '#f59e0b',
  failed: '#dc2626',
}

const FILE_STATUS_COLORS: Record<string, string> = {
  added: '#16a34a',
  modified: '#f59e0b',
  deleted: '#dc2626',
  renamed: '#6366f1',
}

function getCheckpointTypeColor(type: string): string {
  return CHECKPOINT_TYPE_COLORS[type] ?? '#6b7280'
}

function getStatusColor(status: string): string {
  return STATUS_COLORS[status] ?? '#6b7280'
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function SectionHeader({ title }: { title: string }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontWeight: 700,
        color: '#8892a4',
        textTransform: 'uppercase',
        letterSpacing: '0.08em',
        marginBottom: 8,
        paddingBottom: 4,
        borderBottom: '1px solid #2e3347',
      }}
    >
      {title}
    </div>
  )
}

function KVRow({
  label,
  value,
}: {
  label: string
  value: React.ReactNode
}) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'flex-start',
        gap: 8,
        marginBottom: 6,
      }}
    >
      <span style={{ fontSize: 11, color: '#8892a4', flexShrink: 0 }}>
        {label}
      </span>
      <span
        style={{
          fontSize: 11,
          color: '#e2e8f0',
          textAlign: 'right',
          wordBreak: 'break-all',
        }}
      >
        {value}
      </span>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const color = getStatusColor(status)
  return (
    <span
      style={{
        fontSize: 9,
        fontWeight: 700,
        color,
        border: `1px solid ${color}`,
        borderRadius: 3,
        padding: '1px 5px',
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
      }}
    >
      {status}
    </span>
  )
}

function CheckpointTypeBadge({ type }: { type: string }) {
  const color = getCheckpointTypeColor(type)
  return (
    <span
      style={{
        fontSize: 9,
        fontWeight: 600,
        color,
        background: `${color}18`,
        border: `1px solid ${color}40`,
        borderRadius: 3,
        padding: '1px 5px',
        textTransform: 'uppercase',
        letterSpacing: '0.04em',
      }}
    >
      {type.replace('_', ' ')}
    </span>
  )
}

function FileStatusIcon({ status }: { status: string }) {
  const color = FILE_STATUS_COLORS[status] ?? '#6b7280'
  const props = { size: 12, color }
  switch (status) {
    case 'added':
      return <FilePlus {...props} />
    case 'deleted':
      return <FileMinus {...props} />
    case 'renamed':
      return <FileEdit {...props} />
    default:
      return <FileText {...props} />
  }
}

// ─── Checkpoint File List ─────────────────────────────────────────────────────

function CheckpointFileList({
  checkpointId,
}: {
  checkpointId: string
}) {
  const [files, setFiles] = useState<CheckpointFile[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchCheckpointFiles(checkpointId)
      .then((data) => {
        if (!cancelled) setFiles(data)
      })
      .catch((err) => {
        if (!cancelled) setError(normaliseError(err).message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [checkpointId])

  if (loading) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '8px 0',
          color: '#6b7280',
          fontSize: 11,
        }}
      >
        <Loader2 size={12} style={{ animation: 'spin 1s linear infinite' }} />
        Loading files…
      </div>
    )
  }

  if (error) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '8px 0',
          color: '#ef4444',
          fontSize: 11,
        }}
      >
        <AlertCircle size={12} />
        {error}
      </div>
    )
  }

  if (files.length === 0) {
    return (
      <div style={{ fontSize: 11, color: '#4b5563', padding: '6px 0' }}>
        No file changes recorded.
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      {files.map((file) => (
        <div
          key={file.file_id}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 6px',
            background: 'rgba(255,255,255,0.02)',
            borderRadius: 4,
            border: '1px solid #2e3347',
          }}
        >
          <FileStatusIcon status={file.status} />
          <span
            style={{
              flex: 1,
              fontSize: 11,
              color: '#cbd5e1',
              fontFamily: 'monospace',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
            title={file.path}
          >
            {file.old_path ? `${file.old_path} → ${file.path}` : file.path}
          </span>
          {file.size_bytes !== null && (
            <span style={{ fontSize: 9, color: '#4b5563', flexShrink: 0 }}>
              {file.size_bytes < 1024
                ? `${file.size_bytes}B`
                : `${(file.size_bytes / 1024).toFixed(1)}KB`}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

// ─── Checkpoint Detail ────────────────────────────────────────────────────────

function CheckpointDetail({
  checkpoint,
  onRevert,
  reverting,
}: {
  checkpoint: Checkpoint
  onRevert: (cp: Checkpoint) => void
  reverting: boolean
}) {
  const [showFiles, setShowFiles] = useState(false)

  const canRevert =
    checkpoint.status === 'committed' || checkpoint.status === 'verified'

  const formatTs = (ts: string | null) => {
    if (!ts) return '—'
    try {
      return format(new Date(ts), 'MMM d, HH:mm:ss')
    } catch {
      return ts
    }
  }

  const relTs = (ts: string | null) => {
    if (!ts) return ''
    try {
      return formatDistanceToNow(new Date(ts), { addSuffix: true })
    } catch {
      return ''
    }
  }

  return (
    <div style={{ padding: '0 16px 16px' }}>
      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginBottom: 8,
          }}
        >
          <GitCommit size={16} color="#6366f1" />
          <h3
            style={{
              margin: 0,
              fontSize: 13,
              fontWeight: 600,
              color: '#e2e8f0',
              flex: 1,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
            title={checkpoint.message}
          >
            {checkpoint.message || `Checkpoint #${checkpoint.sequence}`}
          </h3>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <StatusBadge status={checkpoint.status} />
          <CheckpointTypeBadge type={checkpoint.checkpoint_type} />
          <span
            style={{
              fontSize: 9,
              color: '#8892a4',
              background: '#1a1d27',
              border: '1px solid #2e3347',
              borderRadius: 3,
              padding: '1px 5px',
            }}
          >
            #{checkpoint.sequence}
          </span>
        </div>
      </div>

      {/* Git info */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="Git" />
        <KVRow
          label="Branch"
          value={
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <GitBranch size={10} />
              {checkpoint.git_branch}
            </span>
          }
        />
        {checkpoint.git_commit_sha && (
          <KVRow
            label="Commit"
            value={
              <span
                style={{ fontFamily: 'monospace', fontSize: 10 }}
                title={checkpoint.git_commit_sha}
              >
                {checkpoint.git_commit_sha.slice(0, 8)}
              </span>
            }
          />
        )}
        {checkpoint.git_tag && (
          <KVRow
            label="Tag"
            value={
              <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <Tag size={10} />
                {checkpoint.git_tag}
              </span>
            }
          />
        )}
      </div>

      {/* Stats */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="Changes" />
        <div
          style={{
            display: 'flex',
            gap: 12,
            padding: '8px 10px',
            background: 'rgba(255,255,255,0.02)',
            borderRadius: 6,
            border: '1px solid #2e3347',
          }}
        >
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: '#e2e8f0' }}>
              {checkpoint.file_count}
            </div>
            <div style={{ fontSize: 9, color: '#6b7280' }}>files</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: '#16a34a' }}>
              +{checkpoint.lines_added}
            </div>
            <div style={{ fontSize: 9, color: '#6b7280' }}>added</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: '#dc2626' }}>
              -{checkpoint.lines_removed}
            </div>
            <div style={{ fontSize: 9, color: '#6b7280' }}>removed</div>
          </div>
          {checkpoint.task_progress !== null && (
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 16, fontWeight: 700, color: '#6366f1' }}>
                {Math.round(checkpoint.task_progress * 100)}%
              </div>
              <div style={{ fontSize: 9, color: '#6b7280' }}>progress</div>
            </div>
          )}
        </div>
      </div>

      {/* Timing */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="Timing" />
        <KVRow
          label="Created"
          value={
            <span title={formatTs(checkpoint.created_at)}>
              {relTs(checkpoint.created_at)}
            </span>
          }
        />
        {checkpoint.verified_at && (
          <KVRow
            label="Verified"
            value={
              <span title={formatTs(checkpoint.verified_at)}>
                {relTs(checkpoint.verified_at)}
              </span>
            }
          />
        )}
        {checkpoint.reverted_at && (
          <KVRow
            label="Reverted"
            value={
              <span title={formatTs(checkpoint.reverted_at)}>
                {relTs(checkpoint.reverted_at)}
              </span>
            }
          />
        )}
      </div>

      {/* IDs */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="References" />
        <KVRow
          label="Checkpoint"
          value={
            <span
              style={{ fontFamily: 'monospace', fontSize: 10 }}
              title={checkpoint.checkpoint_id}
            >
              {checkpoint.checkpoint_id.slice(0, 16)}…
            </span>
          }
        />
        <KVRow
          label="Task"
          value={
            <span
              style={{ fontFamily: 'monospace', fontSize: 10 }}
              title={checkpoint.task_id}
            >
              {checkpoint.task_id.slice(0, 16)}…
            </span>
          }
        />
        <KVRow
          label="Agent"
          value={
            <span
              style={{ fontFamily: 'monospace', fontSize: 10 }}
              title={checkpoint.agent_id}
            >
              {checkpoint.agent_id.slice(0, 16)}…
            </span>
          }
        />
      </div>

      {/* Notes */}
      {checkpoint.notes && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Notes" />
          <p
            style={{
              margin: 0,
              fontSize: 11,
              color: '#cbd5e1',
              lineHeight: 1.5,
              background: 'rgba(255,255,255,0.02)',
              border: '1px solid #2e3347',
              borderRadius: 4,
              padding: '8px 10px',
            }}
          >
            {checkpoint.notes}
          </p>
        </div>
      )}

      {/* Files toggle */}
      <div style={{ marginBottom: 16 }}>
        <button
          onClick={() => setShowFiles((v) => !v)}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            padding: 0,
            marginBottom: 8,
          }}
        >
          {showFiles ? (
            <ChevronDown size={12} color="#8892a4" />
          ) : (
            <ChevronRight size={12} color="#8892a4" />
          )}
          <span
            style={{
              fontSize: 10,
              fontWeight: 700,
              color: '#8892a4',
              textTransform: 'uppercase',
              letterSpacing: '0.08em',
            }}
          >
            Changed Files ({checkpoint.file_count})
          </span>
        </button>
        {showFiles && (
          <CheckpointFileList checkpointId={checkpoint.checkpoint_id} />
        )}
      </div>

      {/* Revert action */}
      {canRevert && (
        <button
          onClick={() => onRevert(checkpoint)}
          disabled={reverting}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            width: '100%',
            padding: '8px 12px',
            background: reverting
              ? 'rgba(239,68,68,0.04)'
              : 'rgba(239,68,68,0.08)',
            border: '1px solid rgba(239,68,68,0.3)',
            borderRadius: 6,
            cursor: reverting ? 'not-allowed' : 'pointer',
            color: '#f87171',
            fontSize: 12,
            fontWeight: 600,
            justifyContent: 'center',
          }}
        >
          {reverting ? (
            <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />
          ) : (
            <RotateCcw size={13} />
          )}
          {reverting ? 'Reverting…' : 'Revert to this Checkpoint'}
        </button>
      )}
    </div>
  )
}

// ─── Checkpoint List Item ─────────────────────────────────────────────────────

const CheckpointListItem = memo(function CheckpointListItem({
  checkpoint,
  isSelected,
  onClick,
}: {
  checkpoint: Checkpoint
  isSelected: boolean
  onClick: () => void
}) {
  const relTs = () => {
    try {
      return formatDistanceToNow(new Date(checkpoint.created_at), {
        addSuffix: true,
      })
    } catch {
      return ''
    }
  }

  return (
    <button
      onClick={onClick}
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 8,
        width: '100%',
        padding: '8px 10px',
        background: isSelected ? 'rgba(99,102,241,0.15)' : 'transparent',
        border: isSelected
          ? '1px solid rgba(99,102,241,0.4)'
          : '1px solid transparent',
        borderRadius: 6,
        cursor: 'pointer',
        textAlign: 'left',
        transition: 'background 0.1s ease',
        marginBottom: 2,
      }}
      onMouseEnter={(e) => {
        if (!isSelected)
          (e.currentTarget as HTMLButtonElement).style.background =
            'rgba(255,255,255,0.04)'
      }}
      onMouseLeave={(e) => {
        if (!isSelected)
          (e.currentTarget as HTMLButtonElement).style.background = 'transparent'
      }}
    >
      <div
        style={{
          width: 28,
          height: 28,
          borderRadius: '50%',
          background: `${getCheckpointTypeColor(checkpoint.checkpoint_type)}20`,
          border: `1px solid ${getCheckpointTypeColor(checkpoint.checkpoint_type)}50`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        <GitCommit
          size={13}
          color={getCheckpointTypeColor(checkpoint.checkpoint_type)}
        />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 12,
            color: '#e2e8f0',
            fontWeight: 500,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            marginBottom: 3,
          }}
          title={checkpoint.message}
        >
          {checkpoint.message || `Checkpoint #${checkpoint.sequence}`}
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            flexWrap: 'wrap',
          }}
        >
          <StatusBadge status={checkpoint.status} />
          <span style={{ fontSize: 9, color: '#4b5563' }}>{relTs()}</span>
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginTop: 3,
          }}
        >
          <span
            style={{
              fontSize: 9,
              color: '#16a34a',
              display: 'flex',
              alignItems: 'center',
              gap: 2,
            }}
          >
            +{checkpoint.lines_added}
          </span>
          <span
            style={{
              fontSize: 9,
              color: '#dc2626',
              display: 'flex',
              alignItems: 'center',
              gap: 2,
            }}
          >
            -{checkpoint.lines_removed}
          </span>
          <span style={{ fontSize: 9, color: '#6b7280' }}>
            {checkpoint.file_count} file{checkpoint.file_count !== 1 ? 's' : ''}
          </span>
        </div>
      </div>
      {isSelected && <ChevronRight size={12} color="#6366f1" />}
    </button>
  )
})

// ─── Main CheckpointViewer Component ─────────────────────────────────────────

export function CheckpointViewer() {
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)

  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [reverting, setReverting] = useState(false)
  const [revertError, setRevertError] = useState<string | null>(null)
  const [revertSuccess, setRevertSuccess] = useState<string | null>(null)

  const selectedCheckpoint = checkpoints.find(
    (cp) => cp.checkpoint_id === selectedId
  ) ?? null

  const load = useCallback(async () => {
    if (!selectedWorkflowId) return
    setLoading(true)
    setError(null)
    try {
      const data = await fetchCheckpoints(selectedWorkflowId)
      setCheckpoints(data)
    } catch (err) {
      setError(normaliseError(err).message)
    } finally {
      setLoading(false)
    }
  }, [selectedWorkflowId])

  useEffect(() => {
    setCheckpoints([])
    setSelectedId(null)
    setRevertError(null)
    setRevertSuccess(null)
    load()
  }, [selectedWorkflowId, load])

  const handleRevert = useCallback(
    async (cp: Checkpoint) => {
      setReverting(true)
      setRevertError(null)
      setRevertSuccess(null)
      try {
        // Use the agent_id from the checkpoint itself as the reverting agent
        await revertCheckpoint(cp.checkpoint_id, cp.agent_id)
        setRevertSuccess(`Reverted to checkpoint #${cp.sequence}`)
        // Refresh the list
        await load()
      } catch (err) {
        setRevertError(normaliseError(err).message)
      } finally {
        setReverting(false)
      }
    },
    [load]
  )

  // ── Empty / no workflow selected ──────────────────────────────────────────

  if (!selectedWorkflowId) {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          gap: 12,
          color: '#4b5563',
        }}
      >
        <GitCommit size={32} />
        <p style={{ margin: 0, fontSize: 13 }}>
          Select a workflow to view checkpoints
        </p>
      </div>
    )
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: '12px 16px 8px',
          borderBottom: '1px solid #2e3347',
          flexShrink: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div>
          <h2
            style={{
              margin: 0,
              fontSize: 13,
              fontWeight: 600,
              color: '#e2e8f0',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <GitCommit size={14} color="#6366f1" />
            Checkpoints
          </h2>
          {selectedWorkflow && (
            <p style={{ margin: 0, fontSize: 11, color: '#8892a4', marginTop: 2 }}>
              {checkpoints.length} checkpoint
              {checkpoints.length !== 1 ? 's' : ''} — {selectedWorkflow.name}
            </p>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {selectedCheckpoint && (
            <button
              onClick={() => setSelectedId(null)}
              style={{
                background: 'transparent',
                border: 'none',
                cursor: 'pointer',
                color: '#8892a4',
                padding: 4,
                borderRadius: 4,
                display: 'flex',
                alignItems: 'center',
              }}
              title="Back to list"
            >
              <X size={14} />
            </button>
          )}
          <button
            onClick={load}
            disabled={loading}
            title="Refresh checkpoints"
            style={{
              background: 'transparent',
              border: 'none',
              cursor: loading ? 'not-allowed' : 'pointer',
              color: '#6b7280',
              padding: 4,
              display: 'flex',
              alignItems: 'center',
            }}
          >
            {loading ? (
              <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />
            ) : (
              <RefreshCw size={13} />
            )}
          </button>
        </div>
      </div>

      {/* Feedback banners */}
      {revertSuccess && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '8px 16px',
            background: 'rgba(22,163,74,0.1)',
            borderBottom: '1px solid rgba(22,163,74,0.3)',
            fontSize: 11,
            color: '#4ade80',
            flexShrink: 0,
          }}
        >
          <CheckCircle2 size={12} />
          {revertSuccess}
          <button
            onClick={() => setRevertSuccess(null)}
            style={{
              marginLeft: 'auto',
              background: 'transparent',
              border: 'none',
              cursor: 'pointer',
              color: '#4ade80',
              padding: 0,
            }}
          >
            <X size={11} />
          </button>
        </div>
      )}
      {revertError && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '8px 16px',
            background: 'rgba(239,68,68,0.1)',
            borderBottom: '1px solid rgba(239,68,68,0.3)',
            fontSize: 11,
            color: '#f87171',
            flexShrink: 0,
          }}
        >
          <AlertCircle size={12} />
          {revertError}
          <button
            onClick={() => setRevertError(null)}
            style={{
              marginLeft: 'auto',
              background: 'transparent',
              border: 'none',
              cursor: 'pointer',
              color: '#f87171',
              padding: 0,
            }}
          >
            <X size={11} />
          </button>
        </div>
      )}

      {/* Content */}
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {error ? (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 8,
              padding: 24,
            }}
          >
            <AlertCircle size={24} color="#ef4444" />
            <p style={{ color: '#ef4444', fontSize: 12, textAlign: 'center', margin: 0 }}>
              {error}
            </p>
            <button
              onClick={load}
              style={{
                fontSize: 11,
                color: '#6366f1',
                background: 'transparent',
                border: '1px solid #6366f1',
                borderRadius: 4,
                padding: '4px 10px',
                cursor: 'pointer',
              }}
            >
              Retry
            </button>
          </div>
        ) : selectedCheckpoint ? (
          <div style={{ paddingTop: 12 }}>
            <CheckpointDetail
              checkpoint={selectedCheckpoint}
              onRevert={handleRevert}
              reverting={reverting}
            />
          </div>
        ) : checkpoints.length === 0 && !loading ? (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 8,
              padding: 32,
              color: '#4b5563',
            }}
          >
            <GitCommit size={28} />
            <p style={{ margin: 0, fontSize: 12, textAlign: 'center' }}>
              No checkpoints yet for this workflow.
            </p>
          </div>
        ) : (
          <div style={{ padding: '8px 8px' }}>
            {checkpoints.map((cp) => (
              <CheckpointListItem
                key={cp.checkpoint_id}
                checkpoint={cp}
                isSelected={selectedId === cp.checkpoint_id}
                onClick={() =>
                  setSelectedId(
                    selectedId === cp.checkpoint_id ? null : cp.checkpoint_id
                  )
                }
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
