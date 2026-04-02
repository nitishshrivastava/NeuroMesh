/**
 * TaskPanel.tsx — FlowOS Task Detail Side Panel
 *
 * Displays detailed information about the selected task:
 *   - Status, type, priority, agent assignment
 *   - Timing information (created, assigned, started, completed)
 *   - Inputs / outputs
 *   - Error details (if failed)
 *   - Dependencies list
 *   - Tags and metadata
 *
 * Also shows a list of all tasks for the selected workflow with
 * status filtering.
 */

import React, { memo } from 'react'
import { formatDistanceToNow, format } from 'date-fns'
import {
  X,
  User,
  Tag,
  AlertTriangle,
  ChevronRight,
  CheckCircle2,
  XCircle,
  Circle,
  PlayCircle,
  PauseCircle,
  ArrowRight,
  Package,
} from 'lucide-react'
import {
  useWorkflowStore,
  selectSelectedTask,
  selectFilteredTasks,
  type Task,
  type TaskStatus,
} from '../../store/workflowStore'
import { STATUS_COLORS, STATUS_BG } from '../../hooks/useWorkflowGraph'

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatTs(ts: string | null): string {
  if (!ts) return '—'
  try {
    return format(new Date(ts), 'MMM d, HH:mm:ss')
  } catch {
    return ts
  }
}

function relativeTs(ts: string | null): string {
  if (!ts) return ''
  try {
    return formatDistanceToNow(new Date(ts), { addSuffix: true })
  } catch {
    return ''
  }
}

// ─── Status Badge ─────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const color = STATUS_COLORS[status as TaskStatus] ?? '#4b5563'
  const bg = STATUS_BG[status as TaskStatus] ?? '#1f2937'
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 700,
        color,
        background: bg,
        border: `1px solid ${color}`,
        borderRadius: 4,
        padding: '2px 6px',
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
      }}
    >
      {status}
    </span>
  )
}

// ─── Status Icon ──────────────────────────────────────────────────────────────

function StatusIcon({ status, size = 14 }: { status: string; size?: number }) {
  const props = { size, strokeWidth: 2 }
  switch (status) {
    case 'completed':
      return <CheckCircle2 {...props} color={STATUS_COLORS.completed} />
    case 'failed':
      return <XCircle {...props} color={STATUS_COLORS.failed} />
    case 'running':
      return <PlayCircle {...props} color={STATUS_COLORS.running} />
    case 'assigned':
    case 'accepted':
      return <PauseCircle {...props} color={STATUS_COLORS.assigned} />
    case 'cancelled':
      return <XCircle {...props} color={STATUS_COLORS.cancelled} />
    default:
      return <Circle {...props} color={STATUS_COLORS.pending} />
  }
}

// ─── Section header ───────────────────────────────────────────────────────────

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

// ─── Key-value row ────────────────────────────────────────────────────────────

function KVRow({ label, value }: { label: string; value: React.ReactNode }) {
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

// ─── Task List Item ───────────────────────────────────────────────────────────

const TaskListItem = memo(function TaskListItem({
  task,
  isSelected,
  onClick,
}: {
  task: Task
  isSelected: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      style={{
        display: 'flex',
        alignItems: 'center',
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
      <StatusIcon status={task.status} size={13} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 12,
            color: '#e2e8f0',
            fontWeight: 500,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {task.name}
        </div>
        <div style={{ fontSize: 10, color: '#8892a4', marginTop: 1 }}>
          {task.task_type}
        </div>
      </div>
      {isSelected && <ChevronRight size={12} color="#6366f1" />}
    </button>
  )
})

// ─── Task Detail View ─────────────────────────────────────────────────────────

function TaskDetail({ task }: { task: Task }) {
  return (
    <div style={{ padding: '0 16px 16px' }}>
      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginBottom: 6,
          }}
        >
          <StatusIcon status={task.status} size={16} />
          <h3
            style={{
              margin: 0,
              fontSize: 14,
              fontWeight: 600,
              color: '#e2e8f0',
            }}
          >
            {task.name}
          </h3>
        </div>
        {task.description && (
          <p style={{ margin: 0, fontSize: 12, color: '#8892a4' }}>
            {task.description}
          </p>
        )}
        <div style={{ marginTop: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <StatusBadge status={task.status} />
          <span
            style={{
              fontSize: 10,
              color: '#8892a4',
              background: '#1a1d27',
              border: '1px solid #2e3347',
              borderRadius: 4,
              padding: '2px 6px',
              textTransform: 'uppercase',
            }}
          >
            {task.task_type}
          </span>
          <span
            style={{
              fontSize: 10,
              color: '#8892a4',
              background: '#1a1d27',
              border: '1px solid #2e3347',
              borderRadius: 4,
              padding: '2px 6px',
            }}
          >
            P: {task.priority}
          </span>
        </div>
      </div>

      {/* Assignment */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="Assignment" />
        <KVRow
          label="Agent"
          value={
            task.assigned_agent_id ? (
              <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <User size={10} />
                {task.assigned_agent_id}
              </span>
            ) : (
              <span style={{ color: '#4b5563' }}>unassigned</span>
            )
          }
        />
        {task.previous_agent_id && (
          <KVRow label="Previous Agent" value={task.previous_agent_id} />
        )}
        {task.workspace_id && (
          <KVRow label="Workspace" value={task.workspace_id} />
        )}
      </div>

      {/* Timing */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="Timing" />
        <KVRow
          label="Created"
          value={
            <span title={formatTs(task.created_at)}>
              {relativeTs(task.created_at)}
            </span>
          }
        />
        {task.assigned_at && (
          <KVRow
            label="Assigned"
            value={
              <span title={formatTs(task.assigned_at)}>
                {relativeTs(task.assigned_at)}
              </span>
            }
          />
        )}
        {task.started_at && (
          <KVRow
            label="Started"
            value={
              <span title={formatTs(task.started_at)}>
                {relativeTs(task.started_at)}
              </span>
            }
          />
        )}
        {task.completed_at && (
          <KVRow
            label="Completed"
            value={
              <span title={formatTs(task.completed_at)}>
                {relativeTs(task.completed_at)}
              </span>
            }
          />
        )}
        <KVRow label="Timeout" value={`${task.timeout_secs}s`} />
        {task.retry_count > 0 && (
          <KVRow
            label="Retries"
            value={`${task.current_retry} / ${task.retry_count}`}
          />
        )}
      </div>

      {/* Dependencies */}
      {task.depends_on.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Dependencies" />
          {task.depends_on.map((depId) => (
            <div
              key={depId}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                marginBottom: 4,
              }}
            >
              <ArrowRight size={10} color="#8892a4" />
              <span
                style={{
                  fontSize: 11,
                  color: '#8892a4',
                  fontFamily: 'monospace',
                }}
              >
                {depId.slice(0, 20)}…
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Inputs */}
      {task.inputs.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Inputs" />
          <pre
            style={{
              margin: 0,
              fontSize: 10,
              color: '#8892a4',
              background: '#0f1117',
              border: '1px solid #2e3347',
              borderRadius: 6,
              padding: 8,
              overflow: 'auto',
              maxHeight: 120,
            }}
          >
            {JSON.stringify(task.inputs, null, 2)}
          </pre>
        </div>
      )}

      {/* Outputs */}
      {task.outputs.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Outputs" />
          <pre
            style={{
              margin: 0,
              fontSize: 10,
              color: '#22c55e',
              background: '#0f1117',
              border: '1px solid #2e3347',
              borderRadius: 6,
              padding: 8,
              overflow: 'auto',
              maxHeight: 120,
            }}
          >
            {JSON.stringify(task.outputs, null, 2)}
          </pre>
        </div>
      )}

      {/* Error */}
      {task.error_message && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Error" />
          <div
            style={{
              background: 'rgba(239,68,68,0.1)',
              border: '1px solid rgba(239,68,68,0.3)',
              borderRadius: 6,
              padding: 10,
            }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 6,
                marginBottom: 6,
              }}
            >
              <AlertTriangle size={12} color="#ef4444" style={{ flexShrink: 0, marginTop: 1 }} />
              <span style={{ fontSize: 11, color: '#ef4444' }}>
                {task.error_message}
              </span>
            </div>
            {Object.keys(task.error_details).length > 0 && (
              <pre
                style={{
                  margin: 0,
                  fontSize: 10,
                  color: '#f87171',
                  overflow: 'auto',
                  maxHeight: 80,
                }}
              >
                {JSON.stringify(task.error_details, null, 2)}
              </pre>
            )}
          </div>
        </div>
      )}

      {/* Tags */}
      {task.tags.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Tags" />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {task.tags.map((tag) => (
              <span
                key={tag}
                style={{
                  fontSize: 10,
                  color: '#8892a4',
                  background: '#1a1d27',
                  border: '1px solid #2e3347',
                  borderRadius: 4,
                  padding: '2px 6px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 3,
                }}
              >
                <Tag size={8} />
                {tag}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Task ID */}
      <div>
        <SectionHeader title="Identifiers" />
        <KVRow
          label="Task ID"
          value={
            <span
              style={{ fontFamily: 'monospace', fontSize: 10, color: '#6b7280' }}
            >
              {task.task_id}
            </span>
          }
        />
        {task.step_id && (
          <KVRow
            label="Step ID"
            value={
              <span
                style={{
                  fontFamily: 'monospace',
                  fontSize: 10,
                  color: '#6b7280',
                }}
              >
                {task.step_id}
              </span>
            }
          />
        )}
      </div>
    </div>
  )
}

// ─── Task Filter Bar ──────────────────────────────────────────────────────────

const FILTER_OPTIONS: Array<{ label: string; value: TaskStatus | null }> = [
  { label: 'All', value: null },
  { label: 'Running', value: 'running' },
  { label: 'Pending', value: 'pending' },
  { label: 'Done', value: 'completed' },
  { label: 'Failed', value: 'failed' },
]

function TaskFilterBar() {
  const taskFilter = useWorkflowStore((s) => s.taskFilter)
  const setTaskFilter = useWorkflowStore((s) => s.setTaskFilter)

  return (
    <div
      style={{
        display: 'flex',
        gap: 4,
        padding: '8px 16px',
        borderBottom: '1px solid #2e3347',
        overflowX: 'auto',
      }}
    >
      {FILTER_OPTIONS.map((opt) => (
        <button
          key={String(opt.value)}
          onClick={() => setTaskFilter(opt.value)}
          style={{
            fontSize: 10,
            fontWeight: 600,
            padding: '3px 8px',
            borderRadius: 4,
            border: 'none',
            cursor: 'pointer',
            background:
              taskFilter === opt.value
                ? 'rgba(99,102,241,0.2)'
                : 'transparent',
            color: taskFilter === opt.value ? '#818cf8' : '#8892a4',
            transition: 'background 0.1s ease',
            whiteSpace: 'nowrap',
          }}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

// ─── Main TaskPanel ───────────────────────────────────────────────────────────

export function TaskPanel() {
  const selectedTask = useWorkflowStore(selectSelectedTask)
  const filteredTasks = useWorkflowStore(selectFilteredTasks)
  const selectedTaskId = useWorkflowStore((s) => s.selectedTaskId)
  const selectTask = useWorkflowStore((s) => s.selectTask)
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        background: '#1a1d27',
        borderLeft: '1px solid #2e3347',
        overflow: 'hidden',
      }}
    >
      {/* Panel header */}
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid #2e3347',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexShrink: 0,
        }}
      >
        <div>
          <h2
            style={{
              margin: 0,
              fontSize: 13,
              fontWeight: 600,
              color: '#e2e8f0',
            }}
          >
            Tasks
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
              {filteredTasks.length} task
              {filteredTasks.length !== 1 ? 's' : ''}
            </p>
          )}
        </div>
        {selectedTask && (
          <button
            onClick={() => selectTask(null)}
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
            title="Close task detail"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {selectedTask ? (
        /* ── Task detail view ─────────────────────────────────────────────── */
        <div style={{ flex: 1, overflowY: 'auto', paddingTop: 12 }}>
          <TaskDetail task={selectedTask} />
        </div>
      ) : (
        /* ── Task list view ───────────────────────────────────────────────── */
        <>
          <TaskFilterBar />
          <div style={{ flex: 1, overflowY: 'auto', padding: '8px 8px' }}>
            {filteredTasks.length === 0 ? (
              <div
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  justifyContent: 'center',
                  height: '100%',
                  gap: 8,
                  padding: 24,
                }}
              >
                <Package size={24} color="#4b5563" />
                <p style={{ color: '#4b5563', fontSize: 12, textAlign: 'center' }}>
                  {selectedWorkflow
                    ? 'No tasks match the current filter'
                    : 'Select a workflow to view tasks'}
                </p>
              </div>
            ) : (
              filteredTasks.map((task) => (
                <TaskListItem
                  key={task.task_id}
                  task={task}
                  isSelected={task.task_id === selectedTaskId}
                  onClick={() => selectTask(task.task_id)}
                />
              ))
            )}
          </div>
        </>
      )}
    </div>
  )
}
