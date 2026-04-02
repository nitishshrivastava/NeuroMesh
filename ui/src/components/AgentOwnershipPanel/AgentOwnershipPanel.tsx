/**
 * AgentOwnershipPanel.tsx — FlowOS Agent Ownership & Status Panel
 *
 * Displays the current ownership state of all agents in the system:
 *   - Agent list with type, status, current task, and capabilities
 *   - Per-agent task history for the selected workflow
 *   - Agent detail view with full metadata
 *   - Live status indicators (online/offline/busy/idle)
 *   - Ownership timeline showing which agent owned each task
 *
 * Data sources:
 *   - GET /agents — all registered agents
 *   - GET /tasks?workflow_id=... — tasks with assigned_agent_id
 *   - Live events from the Zustand store for real-time status updates
 */

import React, { useState, useEffect, useCallback, memo } from 'react'
import { formatDistanceToNow, format } from 'date-fns'
import {
  User,
  Bot,
  Cpu,
  Users,
  RefreshCw,
  ChevronRight,
  AlertCircle,
  Loader2,
  X,
} from 'lucide-react'
import {
  useWorkflowStore,
  type Task,
  type TaskStatus,
} from '../../store/workflowStore'
import { apiClient, normaliseError, type Agent } from '../../api/client'
import { STATUS_COLORS as TASK_STATUS_COLORS } from '../../hooks/useWorkflowGraph'

// ─── Types ────────────────────────────────────────────────────────────────────

interface AgentCapability {
  capability_id: string
  name: string
  version: string | null
  description: string | null
  enabled: boolean
}

interface AgentDetail extends Omit<Agent, 'capabilities'> {
  email?: string | null
  display_name?: string | null
  max_concurrent_tasks?: number
  agent_metadata?: Record<string, unknown>
  capabilities?: AgentCapability[]
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const AGENT_TYPE_COLORS: Record<string, string> = {
  human: '#22c55e',
  machine: '#3b82f6',
  ai: '#a855f7',
}

const AGENT_STATUS_COLORS: Record<string, string> = {
  online: '#22c55e',
  idle: '#22c55e',
  busy: '#f59e0b',
  offline: '#4b5563',
  error: '#ef4444',
}

function getAgentTypeColor(type: string): string {
  return AGENT_TYPE_COLORS[type] ?? '#6b7280'
}

function getAgentStatusColor(status: string): string {
  return AGENT_STATUS_COLORS[status] ?? '#6b7280'
}

function AgentTypeIcon({ type, size = 14 }: { type: string; size?: number }) {
  const color = getAgentTypeColor(type)
  const props = { size, color }
  switch (type) {
    case 'human':
      return <User {...props} />
    case 'machine':
      return <Cpu {...props} />
    case 'ai':
      return <Bot {...props} />
    default:
      return <Users {...props} />
  }
}

function AgentStatusDot({ status }: { status: string }) {
  const color = getAgentStatusColor(status)
  const isActive = status === 'online' || status === 'idle' || status === 'busy'
  return (
    <div
      style={{
        width: 7,
        height: 7,
        borderRadius: '50%',
        background: color,
        boxShadow: isActive ? `0 0 5px ${color}` : 'none',
        flexShrink: 0,
      }}
      title={status}
    />
  )
}

function formatTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  try {
    return format(new Date(ts), 'MMM d, HH:mm:ss')
  } catch {
    return ts
  }
}

function relTs(ts: string | null | undefined): string {
  if (!ts) return ''
  try {
    return formatDistanceToNow(new Date(ts), { addSuffix: true })
  } catch {
    return ''
  }
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

// ─── Task Ownership Row ───────────────────────────────────────────────────────

function TaskOwnershipRow({ task }: { task: Task }) {
  const color = TASK_STATUS_COLORS[task.status as TaskStatus] ?? '#6b7280'

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '5px 8px',
        background: 'rgba(255,255,255,0.02)',
        border: '1px solid #2e3347',
        borderRadius: 4,
        marginBottom: 3,
      }}
    >
      <div
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: color,
          flexShrink: 0,
        }}
      />
      <span
        style={{
          flex: 1,
          fontSize: 11,
          color: '#cbd5e1',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
        title={task.name}
      >
        {task.name}
      </span>
      <span
        style={{
          fontSize: 9,
          fontWeight: 700,
          color,
          textTransform: 'uppercase',
          letterSpacing: '0.04em',
          flexShrink: 0,
        }}
      >
        {task.status}
      </span>
    </div>
  )
}

// ─── Agent Detail View ────────────────────────────────────────────────────────

function AgentDetail({
  agent,
  tasks,
}: {
  agent: AgentDetail
  tasks: Task[]
}) {
  const agentTasks = tasks.filter(
    (t) =>
      t.assigned_agent_id === agent.agent_id ||
      t.previous_agent_id === agent.agent_id
  )

  const currentTask = tasks.find(
    (t) =>
      t.assigned_agent_id === agent.agent_id &&
      (t.status === 'running' || t.status === 'accepted' || t.status === 'assigned')
  )

  return (
    <div style={{ padding: '0 16px 16px' }}>
      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            marginBottom: 8,
          }}
        >
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: '50%',
              background: `${getAgentTypeColor(agent.agent_type)}18`,
              border: `1px solid ${getAgentTypeColor(agent.agent_type)}40`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <AgentTypeIcon type={agent.agent_type} size={16} />
          </div>
          <div>
            <h3
              style={{
                margin: 0,
                fontSize: 14,
                fontWeight: 600,
                color: '#e2e8f0',
              }}
            >
              {agent.name}
            </h3>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                marginTop: 3,
              }}
            >
              <AgentStatusDot status={agent.status} />
              <span
                style={{
                  fontSize: 10,
                  color: getAgentStatusColor(agent.status),
                  fontWeight: 600,
                  textTransform: 'uppercase',
                }}
              >
                {agent.status}
              </span>
              <span
                style={{
                  fontSize: 10,
                  color: getAgentTypeColor(agent.agent_type),
                  background: `${getAgentTypeColor(agent.agent_type)}18`,
                  border: `1px solid ${getAgentTypeColor(agent.agent_type)}40`,
                  borderRadius: 3,
                  padding: '1px 5px',
                  textTransform: 'uppercase',
                  fontWeight: 600,
                }}
              >
                {agent.agent_type}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Current task */}
      {currentTask && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Current Task" />
          <div
            style={{
              padding: '8px 10px',
              background: 'rgba(245,158,11,0.06)',
              border: '1px solid rgba(245,158,11,0.2)',
              borderRadius: 4,
            }}
          >
            <div
              style={{
                fontSize: 12,
                fontWeight: 600,
                color: '#fbbf24',
                marginBottom: 3,
              }}
            >
              {currentTask.name}
            </div>
            <div style={{ fontSize: 10, color: '#d97706' }}>
              {currentTask.task_type} · {currentTask.status}
            </div>
          </div>
        </div>
      )}

      {/* Info */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader title="Info" />
        <KVRow
          label="ID"
          value={
            <span
              style={{ fontFamily: 'monospace', fontSize: 10 }}
              title={agent.agent_id}
            >
              {agent.agent_id.slice(0, 16)}…
            </span>
          }
        />
        {agent.project && <KVRow label="Project" value={agent.project} />}
        {agent.email && <KVRow label="Email" value={agent.email} />}
        <KVRow
          label="Registered"
          value={
            <span title={formatTs(agent.registered_at)}>
              {relTs(agent.registered_at)}
            </span>
          }
        />
        {agent.last_seen_at && (
          <KVRow
            label="Last Seen"
            value={
              <span title={formatTs(agent.last_seen_at)}>
                {relTs(agent.last_seen_at)}
              </span>
            }
          />
        )}
      </div>

      {/* Capabilities */}
      {agent.capabilities && agent.capabilities.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Capabilities" />
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {agent.capabilities.map((cap) => (
              <span
                key={cap.capability_id}
                style={{
                  fontSize: 10,
                  color: cap.enabled ? '#8892a4' : '#374151',
                  background: cap.enabled ? '#1a1d27' : '#111827',
                  border: `1px solid ${cap.enabled ? '#2e3347' : '#1f2937'}`,
                  borderRadius: 3,
                  padding: '2px 6px',
                  textDecoration: cap.enabled ? 'none' : 'line-through',
                }}
                title={cap.description ?? cap.name}
              >
                {cap.name}
                {cap.version && (
                  <span style={{ color: '#4b5563' }}> v{cap.version}</span>
                )}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Tags */}
      {agent.tags.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader title="Tags" />
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {agent.tags.map((tag) => (
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

      {/* Task ownership history */}
      {agentTasks.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <SectionHeader
            title={`Task History (${agentTasks.length})`}
          />
          {agentTasks.map((task) => (
            <TaskOwnershipRow key={task.task_id} task={task} />
          ))}
        </div>
      )}
    </div>
  )
}

// ─── Agent List Item ──────────────────────────────────────────────────────────

const AgentListItem = memo(function AgentListItem({
  agent,
  taskCount,
  isSelected,
  onClick,
}: {
  agent: AgentDetail
  taskCount: number
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
      <div
        style={{
          width: 30,
          height: 30,
          borderRadius: '50%',
          background: `${getAgentTypeColor(agent.agent_type)}18`,
          border: `1px solid ${getAgentTypeColor(agent.agent_type)}40`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
          position: 'relative',
        }}
      >
        <AgentTypeIcon type={agent.agent_type} size={13} />
        {/* Status dot overlay */}
        <div
          style={{
            position: 'absolute',
            bottom: -1,
            right: -1,
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: getAgentStatusColor(agent.status),
            border: '1px solid #1a1d27',
          }}
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
            marginBottom: 2,
          }}
          title={agent.name}
        >
          {agent.name}
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
          }}
        >
          <span
            style={{
              fontSize: 9,
              color: getAgentTypeColor(agent.agent_type),
              textTransform: 'uppercase',
              fontWeight: 600,
            }}
          >
            {agent.agent_type}
          </span>
          <span
            style={{
              fontSize: 9,
              color: getAgentStatusColor(agent.status),
              textTransform: 'uppercase',
            }}
          >
            {agent.status}
          </span>
          {taskCount > 0 && (
            <span style={{ fontSize: 9, color: '#6b7280' }}>
              {taskCount} task{taskCount !== 1 ? 's' : ''}
            </span>
          )}
        </div>
      </div>
      {isSelected && <ChevronRight size={12} color="#6366f1" />}
    </button>
  )
})

// ─── Ownership Timeline ───────────────────────────────────────────────────────

function OwnershipTimeline({ tasks }: { tasks: Task[] }) {
  if (tasks.length === 0) return null

  // Group tasks by agent
  const byAgent = new Map<string, Task[]>()
  tasks.forEach((t) => {
    const agentId = t.assigned_agent_id ?? 'unassigned'
    if (!byAgent.has(agentId)) byAgent.set(agentId, [])
    byAgent.get(agentId)!.push(t)
  })

  return (
    <div
      style={{
        padding: '10px 16px',
        borderBottom: '1px solid #2e3347',
        flexShrink: 0,
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          color: '#8892a4',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          marginBottom: 8,
        }}
      >
        Ownership Overview
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {Array.from(byAgent.entries()).map(([agentId, agentTasks]) => (
          <div
            key={agentId}
            style={{ display: 'flex', alignItems: 'center', gap: 8 }}
          >
            <span
              style={{
                fontSize: 9,
                color: '#6b7280',
                fontFamily: 'monospace',
                width: 80,
                flexShrink: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={agentId}
            >
              {agentId === 'unassigned'
                ? 'unassigned'
                : agentId.slice(0, 10) + '…'}
            </span>
            <div style={{ flex: 1, display: 'flex', gap: 2 }}>
              {agentTasks.map((t) => {
                const color =
                  TASK_STATUS_COLORS[t.status as TaskStatus] ?? '#4b5563'
                return (
                  <div
                    key={t.task_id}
                    style={{
                      flex: 1,
                      height: 12,
                      background: `${color}40`,
                      border: `1px solid ${color}60`,
                      borderRadius: 2,
                      minWidth: 8,
                    }}
                    title={`${t.name} (${t.status})`}
                  />
                )
              })}
            </div>
            <span style={{ fontSize: 9, color: '#4b5563', flexShrink: 0 }}>
              {agentTasks.length}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Main AgentOwnershipPanel Component ──────────────────────────────────────

export function AgentOwnershipPanel() {
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)
  const tasks = useWorkflowStore((s) => s.tasks)
  const liveEvents = useWorkflowStore((s) => s.events)

  const [agents, setAgents] = useState<AgentDetail[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const [typeFilter, setTypeFilter] = useState<string | null>(null)

  const selectedAgent = agents.find((a) => a.agent_id === selectedAgentId) ?? null

  // Apply live event updates to agent status
  const agentsWithLiveStatus = agents.map((agent) => {
    // Find the most recent AGENT_* event for this agent
    const recentEvent = liveEvents.find(
      (e) => e.agent_id === agent.agent_id && e.event_type.startsWith('AGENT_')
    )
    if (!recentEvent) return agent

    let liveStatus = agent.status
    switch (recentEvent.event_type) {
      case 'AGENT_ONLINE':
        liveStatus = 'online'
        break
      case 'AGENT_OFFLINE':
        liveStatus = 'offline'
        break
      case 'AGENT_BUSY':
        liveStatus = 'busy'
        break
      case 'AGENT_IDLE':
        liveStatus = 'idle'
        break
    }
    return { ...agent, status: liveStatus }
  })

  const filteredAgents = typeFilter
    ? agentsWithLiveStatus.filter((a) => a.agent_type === typeFilter)
    : agentsWithLiveStatus

  const loadAgents = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await apiClient.get<{
        items: AgentDetail[]
        total: number
      }>('/agents', {
        params: { limit: 100, offset: 0 },
      })
      setAgents(res.data.items)
    } catch (err) {
      setError(normaliseError(err).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadAgents()
  }, [loadAgents])

  // Count tasks per agent for the selected workflow
  const taskCountByAgent = new Map<string, number>()
  tasks.forEach((t) => {
    if (t.assigned_agent_id) {
      taskCountByAgent.set(
        t.assigned_agent_id,
        (taskCountByAgent.get(t.assigned_agent_id) ?? 0) + 1
      )
    }
  })

  const TYPE_FILTERS = [
    { label: 'All', value: null },
    { label: 'Human', value: 'human' },
    { label: 'Machine', value: 'machine' },
    { label: 'AI', value: 'ai' },
  ]

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
              <Users size={14} color="#6366f1" />
              Agents
            </h2>
            <p
              style={{
                margin: 0,
                fontSize: 11,
                color: '#8892a4',
                marginTop: 2,
              }}
            >
              {filteredAgents.length} agent
              {filteredAgents.length !== 1 ? 's' : ''}
              {selectedWorkflow ? ` — ${selectedWorkflow.name}` : ''}
            </p>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {selectedAgent && (
              <button
                onClick={() => setSelectedAgentId(null)}
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
              onClick={loadAgents}
              disabled={loading}
              title="Refresh agents"
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

        {/* Type filter chips */}
        {!selectedAgent && (
          <div style={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>
            {TYPE_FILTERS.map((f) => (
              <button
                key={String(f.value)}
                onClick={() => setTypeFilter(f.value)}
                style={{
                  fontSize: 9,
                  fontWeight: 600,
                  padding: '2px 6px',
                  borderRadius: 3,
                  border: 'none',
                  cursor: 'pointer',
                  background:
                    typeFilter === f.value
                      ? 'rgba(99,102,241,0.2)'
                      : 'transparent',
                  color: typeFilter === f.value ? '#818cf8' : '#6b7280',
                }}
              >
                {f.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Ownership timeline (only when workflow is selected and tasks exist) */}
      {!selectedAgent && selectedWorkflowId && tasks.length > 0 && (
        <OwnershipTimeline tasks={tasks} />
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
              onClick={loadAgents}
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
        ) : selectedAgent ? (
          <div style={{ paddingTop: 12 }}>
            <AgentDetail agent={selectedAgent} tasks={tasks} />
          </div>
        ) : filteredAgents.length === 0 && !loading ? (
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
            <Users size={28} />
            <p style={{ margin: 0, fontSize: 12, textAlign: 'center' }}>
              {typeFilter
                ? `No ${typeFilter} agents registered.`
                : 'No agents registered.'}
            </p>
          </div>
        ) : (
          <div style={{ padding: '8px 8px' }}>
            {filteredAgents.map((agent) => (
              <AgentListItem
                key={agent.agent_id}
                agent={agent}
                taskCount={taskCountByAgent.get(agent.agent_id) ?? 0}
                isSelected={selectedAgentId === agent.agent_id}
                onClick={() =>
                  setSelectedAgentId(
                    selectedAgentId === agent.agent_id ? null : agent.agent_id
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
