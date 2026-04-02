/**
 * HandoffPanel.tsx — FlowOS Task Handoff Management Panel
 *
 * Displays and manages task ownership transfers between agents.
 * Features:
 *   - List of handoffs for the selected workflow (filterable by status)
 *   - Handoff detail view with context, timing, and agent info
 *   - Accept / Reject / Cancel actions
 *   - Create new handoff request
 *   - Real-time status updates via store events
 *
 * API endpoints used:
 *   GET  /handoffs?workflow_id=...
 *   GET  /handoffs/{id}
 *   POST /handoffs
 *   POST /handoffs/{id}/accept
 *   POST /handoffs/{id}/reject
 *   POST /handoffs/{id}/cancel
 */

import React, { useState, useEffect, useCallback, memo } from 'react'
import { format, formatDistanceToNow } from 'date-fns'
import {
  ArrowRight,
  ArrowLeftRight,
  RefreshCw,
  ChevronRight,
  CheckCircle2,
  XCircle,
  Clock,
  AlertCircle,
  Loader2,
  User,
  X,
  Plus,
  Send,
  Ban,
} from 'lucide-react'
import { useWorkflowStore } from '../../store/workflowStore'
import { apiClient, normaliseError, type Agent } from '../../api/client'

// ─── Types ────────────────────────────────────────────────────────────────────

type HandoffStatus =
  | 'pending'
  | 'accepted'
  | 'completed'
  | 'rejected'
  | 'cancelled'
  | 'failed'
  | string

interface Handoff {
  handoff_id: string
  task_id: string
  workflow_id: string
  source_agent_id: string
  target_agent_id: string | null
  handoff_type: string
  status: HandoffStatus
  checkpoint_id: string | null
  workspace_id: string | null
  context: Record<string, unknown> | null
  rejection_reason: string | null
  failure_reason: string | null
  priority: string
  expires_at: string | null
  accepted_at: string | null
  completed_at: string | null
  tags: string[]
  handoff_metadata: Record<string, unknown>
  requested_at: string
  updated_at: string
}

interface HandoffListResponse {
  items: Handoff[]
  total: number
  limit: number
  offset: number
}

// ─── API helpers ──────────────────────────────────────────────────────────────

async function fetchHandoffs(workflowId: string): Promise<Handoff[]> {
  const res = await apiClient.get<HandoffListResponse>('/handoffs', {
    params: { workflow_id: workflowId, limit: 100, offset: 0 },
  })
  return res.data.items
}

async function acceptHandoff(handoffId: string, agentId: string): Promise<Handoff> {
  const res = await apiClient.post<Handoff>(`/handoffs/${handoffId}/accept`, {
    agent_id: agentId,
  })
  return res.data
}

async function rejectHandoff(
  handoffId: string,
  agentId: string,
  reason?: string
): Promise<Handoff> {
  const res = await apiClient.post<Handoff>(`/handoffs/${handoffId}/reject`, {
    agent_id: agentId,
    reason,
  })
  return res.data
}

async function cancelHandoff(
  handoffId: string,
  reason?: string
): Promise<Handoff> {
  const res = await apiClient.post<Handoff>(`/handoffs/${handoffId}/cancel`, {
    reason,
  })
  return res.data
}

async function createHandoff(body: {
  task_id: string
  workflow_id: string
  source_agent_id: string
  target_agent_id?: string
  handoff_type: string
  context?: Record<string, unknown>
  priority: string
}): Promise<Handoff> {
  const res = await apiClient.post<Handoff>('/handoffs', body)
  return res.data
}

async function fetchAgents(): Promise<Agent[]> {
  const res = await apiClient.get<{ items: Agent[] }>('/agents', {
    params: { limit: 100, offset: 0 },
  })
  return res.data.items
}

// ─── Colour helpers ───────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, string> = {
  pending: '#f59e0b',
  accepted: '#3b82f6',
  completed: '#16a34a',
  rejected: '#dc2626',
  cancelled: '#6b7280',
  failed: '#ef4444',
}

const HANDOFF_TYPE_COLORS: Record<string, string> = {
  delegation: '#6366f1',
  escalation: '#f59e0b',
  collaboration: '#22c55e',
  review: '#a855f7',
  emergency: '#ef4444',
}

function getStatusColor(status: string): string {
  return STATUS_COLORS[status] ?? '#6b7280'
}

function getHandoffTypeColor(type: string): string {
  return HANDOFF_TYPE_COLORS[type] ?? '#6b7280'
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

function HandoffTypeBadge({ type }: { type: string }) {
  const color = getHandoffTypeColor(type)
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
      {type}
    </span>
  )
}

function StatusIcon({ status }: { status: string }) {
  const color = getStatusColor(status)
  const props = { size: 13, color }
  switch (status) {
    case 'completed':
      return <CheckCircle2 {...props} />
    case 'rejected':
    case 'cancelled':
    case 'failed':
      return <XCircle {...props} />
    case 'accepted':
      return <ArrowRight {...props} />
    default:
      return <Clock {...props} />
  }
}

// ─── Create Handoff Form ──────────────────────────────────────────────────────

function CreateHandoffForm({
  workflowId,
  tasks,
  onCreated,
  onCancel,
}: {
  workflowId: string
  tasks: { task_id: string; name: string }[]
  onCreated: (h: Handoff) => void
  onCancel: () => void
}) {
  const [taskId, setTaskId] = useState(tasks[0]?.task_id ?? '')
  const [sourceAgentId, setSourceAgentId] = useState('')
  const [targetAgentId, setTargetAgentId] = useState('')
  const [handoffType, setHandoffType] = useState('delegation')
  const [priority, setPriority] = useState('normal')
  const [contextNote, setContextNote] = useState('')
  const [agents, setAgents] = useState<Agent[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    fetchAgents()
      .then(setAgents)
      .catch(() => {})
  }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!taskId || !sourceAgentId) {
      setFormError('Task and source agent are required.')
      return
    }
    setSubmitting(true)
    setFormError(null)
    try {
      const h = await createHandoff({
        task_id: taskId,
        workflow_id: workflowId,
        source_agent_id: sourceAgentId,
        target_agent_id: targetAgentId || undefined,
        handoff_type: handoffType,
        priority,
        context: contextNote ? { note: contextNote } : undefined,
      })
      onCreated(h)
    } catch (err) {
      setFormError(normaliseError(err).message)
    } finally {
      setSubmitting(false)
    }
  }

  const inputStyle: React.CSSProperties = {
    width: '100%',
    padding: '6px 8px',
    background: '#0f1117',
    border: '1px solid #2e3347',
    borderRadius: 4,
    color: '#e2e8f0',
    fontSize: 11,
    outline: 'none',
    boxSizing: 'border-box',
  }

  const labelStyle: React.CSSProperties = {
    fontSize: 10,
    fontWeight: 600,
    color: '#8892a4',
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    display: 'block',
    marginBottom: 4,
  }

  return (
    <form onSubmit={handleSubmit} style={{ padding: '12px 16px' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 16,
        }}
      >
        <ArrowLeftRight size={14} color="#6366f1" />
        <h3 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: '#e2e8f0' }}>
          New Handoff Request
        </h3>
      </div>

      {formError && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '8px 10px',
            background: 'rgba(239,68,68,0.1)',
            border: '1px solid rgba(239,68,68,0.3)',
            borderRadius: 4,
            fontSize: 11,
            color: '#f87171',
            marginBottom: 12,
          }}
        >
          <AlertCircle size={11} />
          {formError}
        </div>
      )}

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Task</label>
        <select
          value={taskId}
          onChange={(e) => setTaskId(e.target.value)}
          style={inputStyle}
          required
        >
          <option value="">Select task…</option>
          {tasks.map((t) => (
            <option key={t.task_id} value={t.task_id}>
              {t.name}
            </option>
          ))}
        </select>
      </div>

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Source Agent</label>
        <select
          value={sourceAgentId}
          onChange={(e) => setSourceAgentId(e.target.value)}
          style={inputStyle}
          required
        >
          <option value="">Select source agent…</option>
          {agents.map((a) => (
            <option key={a.agent_id} value={a.agent_id}>
              {a.name} ({a.agent_type})
            </option>
          ))}
        </select>
      </div>

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Target Agent (optional)</label>
        <select
          value={targetAgentId}
          onChange={(e) => setTargetAgentId(e.target.value)}
          style={inputStyle}
        >
          <option value="">Any available agent</option>
          {agents.map((a) => (
            <option key={a.agent_id} value={a.agent_id}>
              {a.name} ({a.agent_type})
            </option>
          ))}
        </select>
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
        <div style={{ flex: 1 }}>
          <label style={labelStyle}>Type</label>
          <select
            value={handoffType}
            onChange={(e) => setHandoffType(e.target.value)}
            style={inputStyle}
          >
            <option value="delegation">Delegation</option>
            <option value="escalation">Escalation</option>
            <option value="collaboration">Collaboration</option>
            <option value="review">Review</option>
            <option value="emergency">Emergency</option>
          </select>
        </div>
        <div style={{ flex: 1 }}>
          <label style={labelStyle}>Priority</label>
          <select
            value={priority}
            onChange={(e) => setPriority(e.target.value)}
            style={inputStyle}
          >
            <option value="low">Low</option>
            <option value="normal">Normal</option>
            <option value="high">High</option>
            <option value="critical">Critical</option>
          </select>
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <label style={labelStyle}>Context Note</label>
        <textarea
          value={contextNote}
          onChange={(e) => setContextNote(e.target.value)}
          placeholder="Briefing for the target agent…"
          rows={3}
          style={{
            ...inputStyle,
            resize: 'vertical',
            fontFamily: 'inherit',
            lineHeight: 1.5,
          }}
        />
      </div>

      <div style={{ display: 'flex', gap: 8 }}>
        <button
          type="submit"
          disabled={submitting}
          style={{
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 6,
            padding: '8px 12px',
            background: submitting
              ? 'rgba(99,102,241,0.1)'
              : 'rgba(99,102,241,0.15)',
            border: '1px solid rgba(99,102,241,0.4)',
            borderRadius: 6,
            cursor: submitting ? 'not-allowed' : 'pointer',
            color: '#818cf8',
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          {submitting ? (
            <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />
          ) : (
            <Send size={13} />
          )}
          {submitting ? 'Requesting…' : 'Request Handoff'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          style={{
            padding: '8px 12px',
            background: 'transparent',
            border: '1px solid #2e3347',
            borderRadius: 6,
            cursor: 'pointer',
            color: '#8892a4',
            fontSize: 12,
          }}
        >
          Cancel
        </button>
      </div>
    </form>
  )
}

// ─── Handoff Detail ───────────────────────────────────────────────────────────

function HandoffDetail({
  handoff,
  onAccept,
  onReject,
  onCancel: onCancelHandoff,
  actionLoading,
}: {
  handoff: Handoff
  onAccept: (h: Handoff) => void
  onReject: (h: Handoff) => void
  onCancel: (h: Handoff) => void
  actionLoading: boolean
}) {
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

  const isPending = handoff.status === 'pending'
  const isAccepted = handoff.status === 'accepted'

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
          <ArrowLeftRight size={16} color="#6366f1" />
          <h3
            style={{
              margin: 0,
              fontSize: 13,
              fontWeight: 600,
              color: '#e2e8f0',
            }}
          >
            Handoff Request
          </h3>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <StatusBadge status={handoff.status} />
          <HandoffTypeBadge type={handoff.handoff_type} />
          <span
            style={{
              fontSize: 9,
              color: '#8892a4',
              background: '#1a1d27',
              border: '1px solid #2e3347',
              borderRadius: 3,
              padding: '1px 5px',
              textTransform: 'uppercase',
            }}
          >
            {handoff.priority}
          </span>
        </div>
      </div>

      {/* Agent transfer */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="Transfer" />
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '10px 12px',
            background: 'rgba(255,255,255,0.02)',
            border: '1px solid #2e3347',
            borderRadius: 6,
          }}
        >
          <div style={{ flex: 1, textAlign: 'center' }}>
            <div
              style={{
                fontSize: 9,
                color: '#6b7280',
                textTransform: 'uppercase',
                marginBottom: 4,
              }}
            >
              From
            </div>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 4,
              }}
            >
              <User size={11} color="#8892a4" />
              <span
                style={{
                  fontSize: 10,
                  color: '#cbd5e1',
                  fontFamily: 'monospace',
                }}
                title={handoff.source_agent_id}
              >
                {handoff.source_agent_id.slice(0, 12)}…
              </span>
            </div>
          </div>
          <ArrowRight size={16} color="#6366f1" />
          <div style={{ flex: 1, textAlign: 'center' }}>
            <div
              style={{
                fontSize: 9,
                color: '#6b7280',
                textTransform: 'uppercase',
                marginBottom: 4,
              }}
            >
              To
            </div>
            {handoff.target_agent_id ? (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 4,
                }}
              >
                <User size={11} color="#8892a4" />
                <span
                  style={{
                    fontSize: 10,
                    color: '#cbd5e1',
                    fontFamily: 'monospace',
                  }}
                  title={handoff.target_agent_id}
                >
                  {handoff.target_agent_id.slice(0, 12)}…
                </span>
              </div>
            ) : (
              <span style={{ fontSize: 10, color: '#4b5563' }}>
                Any agent
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Task reference */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="References" />
        <KVRow
          label="Task"
          value={
            <span
              style={{ fontFamily: 'monospace', fontSize: 10 }}
              title={handoff.task_id}
            >
              {handoff.task_id.slice(0, 16)}…
            </span>
          }
        />
        {handoff.checkpoint_id && (
          <KVRow
            label="Checkpoint"
            value={
              <span
                style={{ fontFamily: 'monospace', fontSize: 10 }}
                title={handoff.checkpoint_id}
              >
                {handoff.checkpoint_id.slice(0, 16)}…
              </span>
            }
          />
        )}
        {handoff.workspace_id && (
          <KVRow label="Workspace" value={handoff.workspace_id} />
        )}
      </div>

      {/* Context */}
      {handoff.context && Object.keys(handoff.context).length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Context" />
          <div
            style={{
              background: 'rgba(255,255,255,0.02)',
              border: '1px solid #2e3347',
              borderRadius: 4,
              padding: '8px 10px',
            }}
          >
            {Object.entries(handoff.context).map(([k, v]) => (
              <KVRow
                key={k}
                label={k}
                value={
                  typeof v === 'string'
                    ? v
                    : JSON.stringify(v)
                }
              />
            ))}
          </div>
        </div>
      )}

      {/* Rejection / failure reason */}
      {(handoff.rejection_reason || handoff.failure_reason) && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Reason" />
          <p
            style={{
              margin: 0,
              fontSize: 11,
              color: '#f87171',
              background: 'rgba(239,68,68,0.06)',
              border: '1px solid rgba(239,68,68,0.2)',
              borderRadius: 4,
              padding: '8px 10px',
              lineHeight: 1.5,
            }}
          >
            {handoff.rejection_reason ?? handoff.failure_reason}
          </p>
        </div>
      )}

      {/* Timing */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="Timing" />
        <KVRow
          label="Requested"
          value={
            <span title={formatTs(handoff.requested_at)}>
              {relTs(handoff.requested_at)}
            </span>
          }
        />
        {handoff.accepted_at && (
          <KVRow
            label="Accepted"
            value={
              <span title={formatTs(handoff.accepted_at)}>
                {relTs(handoff.accepted_at)}
              </span>
            }
          />
        )}
        {handoff.completed_at && (
          <KVRow
            label="Completed"
            value={
              <span title={formatTs(handoff.completed_at)}>
                {relTs(handoff.completed_at)}
              </span>
            }
          />
        )}
        {handoff.expires_at && (
          <KVRow
            label="Expires"
            value={
              <span title={formatTs(handoff.expires_at)}>
                {relTs(handoff.expires_at)}
              </span>
            }
          />
        )}
      </div>

      {/* Tags */}
      {handoff.tags.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Tags" />
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {handoff.tags.map((tag) => (
              <span
                key={tag}
                style={{
                  fontSize: 10,
                  color: '#8892a4',
                  background: '#1a1d27',
                  border: '1px solid #2e3347',
                  borderRadius: 3,
                  padding: '2px 6px',
                }}
              >
                {tag}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Actions */}
      {(isPending || isAccepted) && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {isPending && handoff.target_agent_id && (
            <button
              onClick={() => onAccept(handoff)}
              disabled={actionLoading}
              style={{
                flex: 1,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 6,
                padding: '7px 10px',
                background: 'rgba(22,163,74,0.1)',
                border: '1px solid rgba(22,163,74,0.3)',
                borderRadius: 6,
                cursor: actionLoading ? 'not-allowed' : 'pointer',
                color: '#4ade80',
                fontSize: 11,
                fontWeight: 600,
              }}
            >
              {actionLoading ? (
                <Loader2 size={12} style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                <CheckCircle2 size={12} />
              )}
              Accept
            </button>
          )}
          {isPending && (
            <button
              onClick={() => onReject(handoff)}
              disabled={actionLoading}
              style={{
                flex: 1,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 6,
                padding: '7px 10px',
                background: 'rgba(239,68,68,0.08)',
                border: '1px solid rgba(239,68,68,0.3)',
                borderRadius: 6,
                cursor: actionLoading ? 'not-allowed' : 'pointer',
                color: '#f87171',
                fontSize: 11,
                fontWeight: 600,
              }}
            >
              {actionLoading ? (
                <Loader2 size={12} style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                <XCircle size={12} />
              )}
              Reject
            </button>
          )}
          {(isPending || isAccepted) && (
            <button
              onClick={() => onCancelHandoff(handoff)}
              disabled={actionLoading}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 6,
                padding: '7px 10px',
                background: 'transparent',
                border: '1px solid #2e3347',
                borderRadius: 6,
                cursor: actionLoading ? 'not-allowed' : 'pointer',
                color: '#6b7280',
                fontSize: 11,
              }}
            >
              <Ban size={12} />
              Cancel
            </button>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Handoff List Item ────────────────────────────────────────────────────────

const HandoffListItem = memo(function HandoffListItem({
  handoff,
  isSelected,
  onClick,
}: {
  handoff: Handoff
  isSelected: boolean
  onClick: () => void
}) {
  const relTs = () => {
    try {
      return formatDistanceToNow(new Date(handoff.requested_at), {
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
          background: `${getStatusColor(handoff.status)}18`,
          border: `1px solid ${getStatusColor(handoff.status)}40`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        <StatusIcon status={handoff.status} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            marginBottom: 3,
          }}
        >
          <span
            style={{
              fontSize: 11,
              color: '#e2e8f0',
              fontFamily: 'monospace',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
            title={handoff.task_id}
          >
            {handoff.task_id.slice(0, 12)}…
          </span>
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            flexWrap: 'wrap',
          }}
        >
          <StatusBadge status={handoff.status} />
          <HandoffTypeBadge type={handoff.handoff_type} />
          <span style={{ fontSize: 9, color: '#4b5563' }}>{relTs()}</span>
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            marginTop: 3,
          }}
        >
          <span
            style={{
              fontSize: 9,
              color: '#6b7280',
              fontFamily: 'monospace',
            }}
            title={handoff.source_agent_id}
          >
            {handoff.source_agent_id.slice(0, 8)}
          </span>
          <ArrowRight size={9} color="#4b5563" />
          <span
            style={{
              fontSize: 9,
              color: '#6b7280',
              fontFamily: 'monospace',
            }}
            title={handoff.target_agent_id ?? 'any'}
          >
            {handoff.target_agent_id
              ? handoff.target_agent_id.slice(0, 8)
              : 'any'}
          </span>
        </div>
      </div>
      {isSelected && <ChevronRight size={12} color="#6366f1" />}
    </button>
  )
})

// ─── Status Filter Bar ────────────────────────────────────────────────────────

const STATUS_FILTERS = [
  { label: 'All', value: null },
  { label: 'Pending', value: 'pending' },
  { label: 'Accepted', value: 'accepted' },
  { label: 'Done', value: 'completed' },
  { label: 'Rejected', value: 'rejected' },
]

// ─── Main HandoffPanel Component ──────────────────────────────────────────────

export function HandoffPanel() {
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)
  const tasks = useWorkflowStore((s) => s.tasks)

  const [handoffs, setHandoffs] = useState<Handoff[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionSuccess, setActionSuccess] = useState<string | null>(null)

  const selectedHandoff =
    handoffs.find((h) => h.handoff_id === selectedId) ?? null

  const filteredHandoffs = statusFilter
    ? handoffs.filter((h) => h.status === statusFilter)
    : handoffs

  const load = useCallback(async () => {
    if (!selectedWorkflowId) return
    setLoading(true)
    setError(null)
    try {
      const data = await fetchHandoffs(selectedWorkflowId)
      setHandoffs(data)
    } catch (err) {
      setError(normaliseError(err).message)
    } finally {
      setLoading(false)
    }
  }, [selectedWorkflowId])

  useEffect(() => {
    setHandoffs([])
    setSelectedId(null)
    setShowCreate(false)
    setActionError(null)
    setActionSuccess(null)
    load()
  }, [selectedWorkflowId, load])

  const handleAccept = useCallback(
    async (h: Handoff) => {
      if (!h.target_agent_id) return
      setActionLoading(true)
      setActionError(null)
      try {
        const updated = await acceptHandoff(h.handoff_id, h.target_agent_id)
        setHandoffs((prev) =>
          prev.map((x) => (x.handoff_id === updated.handoff_id ? updated : x))
        )
        setActionSuccess('Handoff accepted.')
      } catch (err) {
        setActionError(normaliseError(err).message)
      } finally {
        setActionLoading(false)
      }
    },
    []
  )

  const handleReject = useCallback(
    async (h: Handoff) => {
      const agentId = h.target_agent_id ?? h.source_agent_id
      setActionLoading(true)
      setActionError(null)
      try {
        const updated = await rejectHandoff(
          h.handoff_id,
          agentId,
          'Rejected from UI'
        )
        setHandoffs((prev) =>
          prev.map((x) => (x.handoff_id === updated.handoff_id ? updated : x))
        )
        setActionSuccess('Handoff rejected.')
      } catch (err) {
        setActionError(normaliseError(err).message)
      } finally {
        setActionLoading(false)
      }
    },
    []
  )

  const handleCancel = useCallback(
    async (h: Handoff) => {
      setActionLoading(true)
      setActionError(null)
      try {
        const updated = await cancelHandoff(h.handoff_id, 'Cancelled from UI')
        setHandoffs((prev) =>
          prev.map((x) => (x.handoff_id === updated.handoff_id ? updated : x))
        )
        setActionSuccess('Handoff cancelled.')
      } catch (err) {
        setActionError(normaliseError(err).message)
      } finally {
        setActionLoading(false)
      }
    },
    []
  )

  const handleCreated = useCallback(
    (h: Handoff) => {
      setHandoffs((prev) => [h, ...prev])
      setShowCreate(false)
      setSelectedId(h.handoff_id)
      setActionSuccess('Handoff request created.')
    },
    []
  )

  // ── No workflow selected ──────────────────────────────────────────────────

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
        <ArrowLeftRight size={32} />
        <p style={{ margin: 0, fontSize: 13 }}>
          Select a workflow to view handoffs
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
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 8,
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
              <ArrowLeftRight size={14} color="#6366f1" />
              Handoffs
            </h2>
            {selectedWorkflow && (
              <p
                style={{
                  margin: 0,
                  fontSize: 11,
                  color: '#8892a4',
                  marginTop: 2,
                }}
              >
                {filteredHandoffs.length} handoff
                {filteredHandoffs.length !== 1 ? 's' : ''}
              </p>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {selectedHandoff && (
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
            {!showCreate && !selectedHandoff && (
              <button
                onClick={() => setShowCreate(true)}
                title="New handoff"
                style={{
                  background: 'rgba(99,102,241,0.1)',
                  border: '1px solid rgba(99,102,241,0.3)',
                  borderRadius: 4,
                  cursor: 'pointer',
                  color: '#818cf8',
                  padding: '3px 6px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                  fontSize: 10,
                  fontWeight: 600,
                }}
              >
                <Plus size={11} />
                New
              </button>
            )}
            <button
              onClick={load}
              disabled={loading}
              title="Refresh handoffs"
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
                <Loader2
                  size={13}
                  style={{ animation: 'spin 1s linear infinite' }}
                />
              ) : (
                <RefreshCw size={13} />
              )}
            </button>
          </div>
        </div>

        {/* Status filter chips */}
        {!showCreate && !selectedHandoff && (
          <div style={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>
            {STATUS_FILTERS.map((f) => (
              <button
                key={String(f.value)}
                onClick={() => setStatusFilter(f.value)}
                style={{
                  fontSize: 9,
                  fontWeight: 600,
                  padding: '2px 6px',
                  borderRadius: 3,
                  border: 'none',
                  cursor: 'pointer',
                  background:
                    statusFilter === f.value
                      ? 'rgba(99,102,241,0.2)'
                      : 'transparent',
                  color: statusFilter === f.value ? '#818cf8' : '#6b7280',
                }}
              >
                {f.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Feedback banners */}
      {actionSuccess && (
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
          {actionSuccess}
          <button
            onClick={() => setActionSuccess(null)}
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
      {actionError && (
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
          {actionError}
          <button
            onClick={() => setActionError(null)}
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
        {showCreate ? (
          <CreateHandoffForm
            workflowId={selectedWorkflowId}
            tasks={tasks.map((t) => ({ task_id: t.task_id, name: t.name }))}
            onCreated={handleCreated}
            onCancel={() => setShowCreate(false)}
          />
        ) : error ? (
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
            <p
              style={{
                color: '#ef4444',
                fontSize: 12,
                textAlign: 'center',
                margin: 0,
              }}
            >
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
        ) : selectedHandoff ? (
          <div style={{ paddingTop: 12 }}>
            <HandoffDetail
              handoff={selectedHandoff}
              onAccept={handleAccept}
              onReject={handleReject}
              onCancel={handleCancel}
              actionLoading={actionLoading}
            />
          </div>
        ) : filteredHandoffs.length === 0 && !loading ? (
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
            <ArrowLeftRight size={28} />
            <p style={{ margin: 0, fontSize: 12, textAlign: 'center' }}>
              {statusFilter
                ? `No ${statusFilter} handoffs.`
                : 'No handoffs for this workflow.'}
            </p>
          </div>
        ) : (
          <div style={{ padding: '8px 8px' }}>
            {filteredHandoffs.map((h) => (
              <HandoffListItem
                key={h.handoff_id}
                handoff={h}
                isSelected={selectedId === h.handoff_id}
                onClick={() =>
                  setSelectedId(
                    selectedId === h.handoff_id ? null : h.handoff_id
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
