/**
 * App.tsx — FlowOS UI Root Component
 *
 * Layout:
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │  TopBar (logo, workflow selector, status, actions)          │
 *   ├──────────────┬──────────────────────────────┬───────────────┤
 *   │  Sidebar     │  WorkflowGraph (main canvas) │  Right Panel  │
 *   │  (workflow   │                              │  (tabbed):    │
 *   │   list)      │                              │  Tasks /      │
 *   │              │                              │  Checkpoints /│
 *   │              │                              │  Handoffs /   │
 *   │              │                              │  AI Trace /   │
 *   │              │                              │  Agents /     │
 *   │              │                              │  Replay       │
 *   ├──────────────┴──────────────────────────────┴───────────────┤
 *   │  EventTimeline (live event feed, bottom strip)              │
 *   └─────────────────────────────────────────────────────────────┘
 *
 * The WebSocket connection is established at the app level and feeds
 * the Zustand store. All child components subscribe to the store.
 */

import { useEffect, useCallback, useState } from 'react'
import { formatDistanceToNow } from 'date-fns'
import {
  GitBranch,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Circle,
  PlayCircle,
  PauseCircle,
  Loader2,
  AlertCircle,
  Zap,
  ListTodo,
  GitCommit,
  ArrowLeftRight,
  Brain,
  Users,
  Activity,
  Plus,
} from 'lucide-react'

import { WorkflowGraph } from './components/WorkflowGraph/WorkflowGraph'
import { CreateWorkflowModal } from './components/CreateWorkflowModal/CreateWorkflowModal'
import { TaskPanel } from './components/TaskPanel/TaskPanel'
import { EventTimeline } from './components/EventTimeline/EventTimeline'
import { CheckpointViewer } from './components/CheckpointViewer/CheckpointViewer'
import { HandoffPanel } from './components/HandoffPanel/HandoffPanel'
import { AITraceViewer } from './components/AITraceViewer/AITraceViewer'
import { ReplayControls } from './components/ReplayControls/ReplayControls'
import { AgentOwnershipPanel } from './components/AgentOwnershipPanel/AgentOwnershipPanel'
import { useWorkflowSocket } from './hooks/useWorkflowSocket'
import {
  useWorkflowStore,
  type Workflow,
  type WorkflowStatus,
} from './store/workflowStore'
import {
  listWorkflows,
  normaliseError,
  cancelWorkflow,
  pauseWorkflow,
  resumeWorkflow,
} from './api/client'

// ─── Workflow status helpers ──────────────────────────────────────────────────

const STATUS_COLORS: Record<WorkflowStatus, string> = {
  pending: '#4b5563',
  running: '#d97706',
  paused: '#6366f1',
  completed: '#16a34a',
  failed: '#dc2626',
  cancelled: '#6b7280',
}

function WorkflowStatusIcon({
  status,
  size = 12,
}: {
  status: WorkflowStatus
  size?: number
}) {
  const props = { size, strokeWidth: 2 }
  switch (status) {
    case 'completed':
      return <CheckCircle2 {...props} color={STATUS_COLORS.completed} />
    case 'failed':
      return <XCircle {...props} color={STATUS_COLORS.failed} />
    case 'running':
      return <PlayCircle {...props} color={STATUS_COLORS.running} />
    case 'paused':
      return <PauseCircle {...props} color={STATUS_COLORS.paused} />
    case 'cancelled':
      return <XCircle {...props} color={STATUS_COLORS.cancelled} />
    default:
      return <Circle {...props} color={STATUS_COLORS.pending} />
  }
}

// ─── Workflow List Item ───────────────────────────────────────────────────────

function WorkflowListItem({
  workflow,
  isSelected,
  onClick,
}: {
  workflow: Workflow
  isSelected: boolean
  onClick: () => void
}) {
  const relTime = (() => {
    try {
      return formatDistanceToNow(new Date(workflow.created_at), {
        addSuffix: true,
      })
    } catch {
      return ''
    }
  })()

  return (
    <button
      onClick={onClick}
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 8,
        width: '100%',
        padding: '10px 12px',
        background: isSelected ? 'rgba(99,102,241,0.15)' : 'transparent',
        border: isSelected
          ? '1px solid rgba(99,102,241,0.4)'
          : '1px solid transparent',
        borderRadius: 6,
        cursor: 'pointer',
        textAlign: 'left',
        marginBottom: 2,
        transition: 'background 0.1s ease',
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
      <WorkflowStatusIcon status={workflow.status as WorkflowStatus} size={13} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 12,
            fontWeight: 600,
            color: '#e2e8f0',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
          title={workflow.name}
        >
          {workflow.name}
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            marginTop: 3,
          }}
        >
          <span
            style={{
              fontSize: 9,
              fontWeight: 700,
              color: STATUS_COLORS[workflow.status as WorkflowStatus] ?? '#6b7280',
              textTransform: 'uppercase',
            }}
          >
            {workflow.status}
          </span>
          <span style={{ fontSize: 9, color: '#4b5563' }}>{relTime}</span>
        </div>
        {workflow.project && (
          <div
            style={{
              fontSize: 9,
              color: '#6b7280',
              marginTop: 2,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {workflow.project}
          </div>
        )}
      </div>
    </button>
  )
}

// ─── Sidebar ──────────────────────────────────────────────────────────────────

function Sidebar() {
  const workflows = useWorkflowStore((s) => s.workflows)
  const workflowsLoading = useWorkflowStore((s) => s.workflowsLoading)
  const workflowsError = useWorkflowStore((s) => s.workflowsError)
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)
  const selectWorkflow = useWorkflowStore((s) => s.selectWorkflow)
  const setWorkflows = useWorkflowStore((s) => s.setWorkflows)
  const setWorkflowsLoading = useWorkflowStore((s) => s.setWorkflowsLoading)
  const setWorkflowsError = useWorkflowStore((s) => s.setWorkflowsError)

  const [statusFilter, setStatusFilter] = useState<string | null>(null)

  const fetchWorkflows = useCallback(async () => {
    setWorkflowsLoading(true)
    setWorkflowsError(null)
    try {
      const res = await listWorkflows({
        status: statusFilter ?? undefined,
        limit: 50,
      })
      setWorkflows(res.items, res.total)
    } catch (err) {
      setWorkflowsError(normaliseError(err).message)
    } finally {
      setWorkflowsLoading(false)
    }
  }, [statusFilter, setWorkflows, setWorkflowsLoading, setWorkflowsError])

  // Initial load + refresh on filter change
  useEffect(() => {
    fetchWorkflows()
  }, [fetchWorkflows])

  const STATUS_FILTERS = [
    { label: 'All', value: null },
    { label: 'Running', value: 'running' },
    { label: 'Pending', value: 'pending' },
    { label: 'Done', value: 'completed' },
    { label: 'Failed', value: 'failed' },
  ]

  return (
    <div
      style={{
        width: 220,
        flexShrink: 0,
        display: 'flex',
        flexDirection: 'column',
        background: '#1a1d27',
        borderRight: '1px solid #2e3347',
        overflow: 'hidden',
      }}
    >
      {/* Sidebar header */}
      <div
        style={{
          padding: '12px 12px 8px',
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
          <span style={{ fontSize: 12, fontWeight: 600, color: '#e2e8f0' }}>
            Workflows
          </span>
          <button
            onClick={fetchWorkflows}
            disabled={workflowsLoading}
            title="Refresh workflows"
            style={{
              background: 'transparent',
              border: 'none',
              cursor: workflowsLoading ? 'not-allowed' : 'pointer',
              color: '#6b7280',
              padding: 2,
              display: 'flex',
              alignItems: 'center',
            }}
          >
            {workflowsLoading ? (
              <Loader2 size={12} style={{ animation: 'spin 1s linear infinite' }} />
            ) : (
              <RefreshCw size={12} />
            )}
          </button>
        </div>

        {/* Status filter chips */}
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
      </div>

      {/* Workflow list */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '6px 6px' }}>
        {workflowsError ? (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 6,
              padding: 16,
            }}
          >
            <AlertCircle size={20} color="#ef4444" />
            <p style={{ color: '#ef4444', fontSize: 11, textAlign: 'center' }}>
              {workflowsError}
            </p>
            <button
              onClick={fetchWorkflows}
              style={{
                fontSize: 10,
                color: '#6366f1',
                background: 'transparent',
                border: '1px solid #6366f1',
                borderRadius: 4,
                padding: '3px 8px',
                cursor: 'pointer',
              }}
            >
              Retry
            </button>
          </div>
        ) : workflows.length === 0 && !workflowsLoading ? (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 6,
              padding: 16,
              color: '#4b5563',
            }}
          >
            <GitBranch size={20} />
            <p style={{ fontSize: 11, textAlign: 'center', margin: 0 }}>
              No workflows found
            </p>
          </div>
        ) : (
          workflows.map((wf) => (
            <WorkflowListItem
              key={wf.workflow_id}
              workflow={wf}
              isSelected={selectedWorkflowId === wf.workflow_id}
              onClick={() =>
                selectWorkflow(
                  selectedWorkflowId === wf.workflow_id ? null : wf.workflow_id
                )
              }
            />
          ))
        )}
      </div>
    </div>
  )
}

// ─── Right Panel Tabs ─────────────────────────────────────────────────────────

type RightPanelTab =
  | 'tasks'
  | 'checkpoints'
  | 'handoffs'
  | 'ai-trace'
  | 'agents'
  | 'replay'

interface TabDef {
  id: RightPanelTab
  label: string
  icon: React.ReactNode
  title: string
}

const RIGHT_PANEL_TABS: TabDef[] = [
  {
    id: 'tasks',
    label: 'Tasks',
    icon: <ListTodo size={13} />,
    title: 'Task list and detail',
  },
  {
    id: 'checkpoints',
    label: 'Checkpoints',
    icon: <GitCommit size={13} />,
    title: 'Git checkpoint history',
  },
  {
    id: 'handoffs',
    label: 'Handoffs',
    icon: <ArrowLeftRight size={13} />,
    title: 'Task ownership transfers',
  },
  {
    id: 'ai-trace',
    label: 'AI Trace',
    icon: <Brain size={13} />,
    title: 'AI reasoning trace',
  },
  {
    id: 'agents',
    label: 'Agents',
    icon: <Users size={13} />,
    title: 'Agent ownership panel',
  },
  {
    id: 'replay',
    label: 'Replay',
    icon: <Activity size={13} />,
    title: 'Event replay controls',
  },
]

// ─── Right Panel with Tab Navigation ─────────────────────────────────────────

function RightPanel() {
  const [activeTab, setActiveTab] = useState<RightPanelTab>('tasks')

  return (
    <div
      style={{
        width: 300,
        flexShrink: 0,
        display: 'flex',
        flexDirection: 'column',
        background: '#1a1d27',
        borderLeft: '1px solid #2e3347',
        overflow: 'hidden',
      }}
    >
      {/* Tab bar */}
      <div
        style={{
          display: 'flex',
          borderBottom: '1px solid #2e3347',
          flexShrink: 0,
          overflowX: 'auto',
          scrollbarWidth: 'none',
        }}
      >
        {RIGHT_PANEL_TABS.map((tab) => {
          const isActive = activeTab === tab.id
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              title={tab.title}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 4,
                padding: '8px 10px',
                background: isActive
                  ? 'rgba(99,102,241,0.12)'
                  : 'transparent',
                border: 'none',
                borderBottom: isActive
                  ? '2px solid #6366f1'
                  : '2px solid transparent',
                cursor: 'pointer',
                color: isActive ? '#818cf8' : '#6b7280',
                fontSize: 10,
                fontWeight: isActive ? 700 : 500,
                whiteSpace: 'nowrap',
                transition: 'color 0.1s ease, background 0.1s ease',
                flexShrink: 0,
              }}
              onMouseEnter={(e) => {
                if (!isActive)
                  (e.currentTarget as HTMLButtonElement).style.color = '#8892a4'
              }}
              onMouseLeave={(e) => {
                if (!isActive)
                  (e.currentTarget as HTMLButtonElement).style.color = '#6b7280'
              }}
            >
              {tab.icon}
              <span>{tab.label}</span>
            </button>
          )
        })}
      </div>

      {/* Tab content */}
      <div style={{ flex: 1, overflow: 'hidden' }}>
        {activeTab === 'tasks' && <TaskPanel />}
        {activeTab === 'checkpoints' && <CheckpointViewer />}
        {activeTab === 'handoffs' && <HandoffPanel />}
        {activeTab === 'ai-trace' && <AITraceViewer />}
        {activeTab === 'agents' && <AgentOwnershipPanel />}
        {activeTab === 'replay' && <ReplayControls />}
      </div>
    </div>
  )
}

// ─── TopBar ───────────────────────────────────────────────────────────────────

function TopBar() {
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)
  const wsStatus = useWorkflowStore((s) => s.wsStatus)
  const upsertWorkflow = useWorkflowStore((s) => s.upsertWorkflow)

  const [actionLoading, setActionLoading] = useState(false)
  const [showCreateModal, setShowCreateModal] = useState(false)

  const handleCancel = useCallback(async () => {
    if (!selectedWorkflow) return
    setActionLoading(true)
    try {
      const updated = await cancelWorkflow(selectedWorkflow.workflow_id)
      upsertWorkflow(updated)
    } catch (err) {
      console.error('Cancel failed:', normaliseError(err).message)
    } finally {
      setActionLoading(false)
    }
  }, [selectedWorkflow, upsertWorkflow])

  const handlePause = useCallback(async () => {
    if (!selectedWorkflow) return
    setActionLoading(true)
    try {
      const updated = await pauseWorkflow(selectedWorkflow.workflow_id)
      upsertWorkflow(updated)
    } catch (err) {
      console.error('Pause failed:', normaliseError(err).message)
    } finally {
      setActionLoading(false)
    }
  }, [selectedWorkflow, upsertWorkflow])

  const handleResume = useCallback(async () => {
    if (!selectedWorkflow) return
    setActionLoading(true)
    try {
      const updated = await resumeWorkflow(selectedWorkflow.workflow_id)
      upsertWorkflow(updated)
    } catch (err) {
      console.error('Resume failed:', normaliseError(err).message)
    } finally {
      setActionLoading(false)
    }
  }, [selectedWorkflow, upsertWorkflow])

  return (
    <div
      style={{
        height: 48,
        background: '#1a1d27',
        borderBottom: '1px solid #2e3347',
        display: 'flex',
        alignItems: 'center',
        padding: '0 16px',
        gap: 12,
        flexShrink: 0,
      }}
    >
      {/* Logo */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Zap size={18} color="#6366f1" />
        <span
          style={{
            fontSize: 15,
            fontWeight: 700,
            color: '#e2e8f0',
            letterSpacing: '-0.02em',
          }}
        >
          FlowOS
        </span>
      </div>

      <div
        style={{ width: 1, height: 20, background: '#2e3347', flexShrink: 0 }}
      />

      {/* Selected workflow info */}
      {selectedWorkflow ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1 }}>
          <WorkflowStatusIcon
            status={selectedWorkflow.status as WorkflowStatus}
            size={14}
          />
          <span
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: '#e2e8f0',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: 300,
            }}
          >
            {selectedWorkflow.name}
          </span>
          <span
            style={{
              fontSize: 10,
              fontWeight: 700,
              color:
                STATUS_COLORS[selectedWorkflow.status as WorkflowStatus] ??
                '#6b7280',
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
            }}
          >
            {selectedWorkflow.status}
          </span>
          {selectedWorkflow.project && (
            <span
              style={{
                fontSize: 10,
                color: '#6b7280',
                background: '#1a1d27',
                border: '1px solid #2e3347',
                borderRadius: 3,
                padding: '1px 6px',
              }}
            >
              {selectedWorkflow.project}
            </span>
          )}
        </div>
      ) : (
        <div style={{ flex: 1 }}>
          <span style={{ fontSize: 12, color: '#4b5563' }}>
            Select a workflow from the sidebar
          </span>
        </div>
      )}

      {/* Action buttons */}
      {selectedWorkflow && selectedWorkflow.status === 'running' && (
        <button
          onClick={handlePause}
          disabled={actionLoading}
          style={{
            fontSize: 11,
            fontWeight: 600,
            padding: '4px 10px',
            borderRadius: 5,
            border: '1px solid rgba(99,102,241,0.4)',
            background: 'rgba(99,102,241,0.1)',
            color: '#818cf8',
            cursor: actionLoading ? 'not-allowed' : 'pointer',
          }}
        >
          Pause
        </button>
      )}
      {selectedWorkflow && selectedWorkflow.status === 'paused' && (
        <button
          onClick={handleResume}
          disabled={actionLoading}
          style={{
            fontSize: 11,
            fontWeight: 600,
            padding: '4px 10px',
            borderRadius: 5,
            border: '1px solid rgba(99,102,241,0.4)',
            background: 'rgba(99,102,241,0.1)',
            color: '#818cf8',
            cursor: actionLoading ? 'not-allowed' : 'pointer',
          }}
        >
          Resume
        </button>
      )}
      {selectedWorkflow &&
        ['running', 'paused', 'pending'].includes(
          selectedWorkflow.status
        ) && (
          <button
            onClick={handleCancel}
            disabled={actionLoading}
            style={{
              fontSize: 11,
              fontWeight: 600,
              padding: '4px 10px',
              borderRadius: 5,
              border: '1px solid rgba(239,68,68,0.3)',
              background: 'rgba(239,68,68,0.08)',
              color: '#f87171',
              cursor: actionLoading ? 'not-allowed' : 'pointer',
            }}
          >
            Cancel
          </button>
        )}

      {/* + New Workflow button */}
      <button
        onClick={() => setShowCreateModal(true)}
        title="Create a new workflow"
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 5,
          fontSize: 11,
          fontWeight: 700,
          padding: '5px 12px',
          borderRadius: 6,
          border: '1px solid rgba(99,102,241,0.5)',
          background: 'rgba(99,102,241,0.12)',
          color: '#818cf8',
          cursor: 'pointer',
          transition: 'all 0.15s ease',
          flexShrink: 0,
        }}
        onMouseEnter={(e) => {
          ;(e.currentTarget as HTMLButtonElement).style.background =
            'rgba(99,102,241,0.22)'
          ;(e.currentTarget as HTMLButtonElement).style.borderColor =
            'rgba(99,102,241,0.7)'
          ;(e.currentTarget as HTMLButtonElement).style.color = '#a5b4fc'
        }}
        onMouseLeave={(e) => {
          ;(e.currentTarget as HTMLButtonElement).style.background =
            'rgba(99,102,241,0.12)'
          ;(e.currentTarget as HTMLButtonElement).style.borderColor =
            'rgba(99,102,241,0.5)'
          ;(e.currentTarget as HTMLButtonElement).style.color = '#818cf8'
        }}
      >
        <Plus size={12} strokeWidth={2.5} />
        New Workflow
      </button>

      {/* WS status dot */}
      <div
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          background:
            wsStatus === 'connected'
              ? '#22c55e'
              : wsStatus === 'connecting'
              ? '#f59e0b'
              : wsStatus === 'error'
              ? '#ef4444'
              : '#4b5563',
          boxShadow:
            wsStatus === 'connected'
              ? '0 0 6px rgba(34,197,94,0.6)'
              : 'none',
        }}
        title={`WebSocket: ${wsStatus}`}
      />

      {/* Create Workflow Modal */}
      <CreateWorkflowModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
      />
    </div>
  )
}

// ─── Root App ─────────────────────────────────────────────────────────────────

export default function App() {
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)

  // Establish WebSocket connection (scoped to selected workflow when one is selected)
  useWorkflowSocket({
    workflowId: selectedWorkflowId ?? undefined,
  })

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100vh',
        background: '#0f1117',
        overflow: 'hidden',
      }}
    >
      {/* Top navigation bar */}
      <TopBar />

      {/* Main content area */}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Left sidebar — workflow list */}
        <Sidebar />

        {/* Centre — workflow graph + event timeline */}
        <div
          style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {/* Workflow graph canvas */}
          <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>
            <WorkflowGraph />
          </div>

          {/* Event timeline strip */}
          <div style={{ height: 260, flexShrink: 0 }}>
            <EventTimeline />
          </div>
        </div>

        {/* Right panel — tabbed: Tasks / Checkpoints / Handoffs / AI Trace / Agents / Replay */}
        <RightPanel />
      </div>
    </div>
  )
}
