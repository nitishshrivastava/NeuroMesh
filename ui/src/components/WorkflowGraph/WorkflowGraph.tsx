/**
 * WorkflowGraph.tsx — FlowOS Workflow DAG Visualisation
 *
 * Renders the selected workflow's tasks as an interactive directed acyclic
 * graph using @xyflow/react. Each node represents a task; edges represent
 * dependencies. Nodes are coloured by task status and update in real-time
 * as the workflow progresses.
 *
 * Features:
 *   - Custom TaskNode with status badge, agent assignment, and type label
 *   - Animated edges for running tasks
 *   - MiniMap for large graphs
 *   - Controls (zoom, fit, lock)
 *   - Click-to-select task (opens TaskPanel)
 *   - Empty state when no workflow is selected
 */

import React, { useCallback, memo } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  BackgroundVariant,
  useNodesState,
  useEdgesState,
  useReactFlow,
  ReactFlowProvider,
  type Node,
  type NodeProps,
  type NodeMouseHandler,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { useWorkflowStore } from '../../store/workflowStore'
import {
  useWorkflowGraph,
  STATUS_COLORS,
  STATUS_BG,
  type TaskNodeData,
} from '../../hooks/useWorkflowGraph'
import {
  Loader2,
  AlertCircle,
  GitBranch,
  User,
  Cpu,
  Bot,
  Clock,
  CheckCircle2,
  XCircle,
  Circle,
  PlayCircle,
  PauseCircle,
} from 'lucide-react'

// ─── Custom node type definition ──────────────────────────────────────────────

// In @xyflow/react v12, custom node types must extend Node<DataType>
export type TaskFlowNode = Node<TaskNodeData, 'taskNode'>

// ─── Task Status Icon ─────────────────────────────────────────────────────────

function TaskStatusIcon({
  status,
  size = 14,
}: {
  status: string
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
    case 'assigned':
    case 'accepted':
      return <PauseCircle {...props} color={STATUS_COLORS.assigned} />
    case 'cancelled':
      return <XCircle {...props} color={STATUS_COLORS.cancelled} />
    default:
      return <Circle {...props} color={STATUS_COLORS.pending} />
  }
}

// ─── Agent Type Icon ──────────────────────────────────────────────────────────

function AgentTypeIcon({ agentId }: { agentId: string | null }) {
  if (!agentId) return <User size={11} color="#6b7280" />
  if (agentId.startsWith('machine') || agentId.startsWith('build'))
    return <Cpu size={11} color="#6b7280" />
  if (agentId.startsWith('ai') || agentId.startsWith('llm'))
    return <Bot size={11} color="#6b7280" />
  return <User size={11} color="#6b7280" />
}

// ─── Custom Task Node ─────────────────────────────────────────────────────────

const TaskNode = memo(function TaskNode({
  data,
  selected,
}: NodeProps<TaskFlowNode>) {
  const { task } = data
  const borderColor = selected
    ? '#6366f1'
    : STATUS_COLORS[task.status as keyof typeof STATUS_COLORS] ?? '#4b5563'
  const bgColor =
    STATUS_BG[task.status as keyof typeof STATUS_BG] ?? '#1f2937'

  return (
    <div
      style={{
        background: bgColor,
        border: `2px solid ${borderColor}`,
        borderRadius: 10,
        padding: '10px 12px',
        width: 200,
        height: 80,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'space-between',
        cursor: 'pointer',
        boxShadow: selected
          ? `0 0 0 3px rgba(99,102,241,0.3), 0 4px 12px rgba(0,0,0,0.4)`
          : '0 2px 8px rgba(0,0,0,0.3)',
        transition: 'box-shadow 0.15s ease, border-color 0.15s ease',
      }}
    >
      {/* Top row: status icon + name */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <TaskStatusIcon status={task.status} size={14} />
        <span
          style={{
            color: '#e2e8f0',
            fontSize: 12,
            fontWeight: 600,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            flex: 1,
          }}
          title={task.name}
        >
          {task.name}
        </span>
      </div>

      {/* Middle: task type */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span
          style={{
            fontSize: 10,
            color: '#8892a4',
            background: '#0f1117',
            borderRadius: 4,
            padding: '1px 5px',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
          }}
        >
          {task.task_type}
        </span>
      </div>

      {/* Bottom row: agent + status badge */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <AgentTypeIcon agentId={task.assigned_agent_id} />
          <span
            style={{
              fontSize: 10,
              color: '#8892a4',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: 90,
            }}
            title={task.assigned_agent_id ?? 'unassigned'}
          >
            {task.assigned_agent_id
              ? task.assigned_agent_id.slice(0, 12) + '…'
              : 'unassigned'}
          </span>
        </div>
        <span
          style={{
            fontSize: 9,
            fontWeight: 700,
            color: borderColor,
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
          }}
        >
          {task.status}
        </span>
      </div>
    </div>
  )
})

// ─── Node types registry ──────────────────────────────────────────────────────

const nodeTypes = { taskNode: TaskNode }

// ─── Inner graph component (needs ReactFlowProvider context) ──────────────────

function WorkflowGraphInner() {
  const { nodes: derivedNodes, edges: derivedEdges, loading, error } =
    useWorkflowGraph()

  const [nodes, setNodes, onNodesChange] = useNodesState<TaskFlowNode>(
    derivedNodes as TaskFlowNode[]
  )
  const [edges, setEdges, onEdgesChange] = useEdgesState(derivedEdges)

  const { fitView } = useReactFlow()

  const selectTask = useWorkflowStore((s) => s.selectTask)
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)

  // Sync derived nodes/edges into local state
  React.useEffect(() => {
    setNodes(derivedNodes as TaskFlowNode[])
    setEdges(derivedEdges)
  }, [derivedNodes, derivedEdges, setNodes, setEdges])

  // Fit view when nodes change
  React.useEffect(() => {
    if (derivedNodes.length > 0) {
      setTimeout(() => fitView({ padding: 0.2, duration: 400 }), 50)
    }
  }, [selectedWorkflowId, derivedNodes.length, fitView])

  const onNodeClick: NodeMouseHandler<TaskFlowNode> = useCallback(
    (_evt, node) => {
      selectTask(node.id)
    },
    [selectTask]
  )

  const onPaneClick = useCallback(() => {
    selectTask(null)
  }, [selectTask])

  // ── Empty state ────────────────────────────────────────────────────────────
  if (!selectedWorkflowId) {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          gap: 16,
          textAlign: 'center',
          background: '#0f1117',
        }}
      >
        <GitBranch size={48} color="#4b5563" />
        <div>
          <p style={{ color: '#8892a4', fontSize: 16, fontWeight: 500, margin: 0 }}>
            No workflow selected
          </p>
          <p style={{ color: '#4b5563', fontSize: 13, marginTop: 4, margin: '4px 0 0' }}>
            Select a workflow from the sidebar to view its task graph
          </p>
        </div>
      </div>
    )
  }

  // ── Loading state ──────────────────────────────────────────────────────────
  if (loading && nodes.length === 0) {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          gap: 12,
          background: '#0f1117',
        }}
      >
        <Loader2 size={32} color="#6366f1" style={{ animation: 'spin 1s linear infinite' }} />
        <p style={{ color: '#8892a4', fontSize: 14, margin: 0 }}>Loading workflow graph…</p>
      </div>
    )
  }

  // ── Error state ────────────────────────────────────────────────────────────
  if (error && nodes.length === 0) {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          gap: 12,
          background: '#0f1117',
        }}
      >
        <AlertCircle size={32} color="#ef4444" />
        <p style={{ color: '#ef4444', fontSize: 14, margin: 0 }}>{error}</p>
      </div>
    )
  }

  // ── Empty tasks state ──────────────────────────────────────────────────────
  if (!loading && nodes.length === 0) {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          gap: 12,
          background: '#0f1117',
        }}
      >
        <Clock size={32} color="#4b5563" />
        <p style={{ color: '#8892a4', fontSize: 14, margin: 0 }}>
          No tasks yet for this workflow
        </p>
        {selectedWorkflow && (
          <span
            style={{
              fontSize: 12,
              color: '#6366f1',
              background: 'rgba(99,102,241,0.1)',
              padding: '2px 8px',
              borderRadius: 4,
            }}
          >
            {selectedWorkflow.status.toUpperCase()}
          </span>
        )}
      </div>
    )
  }

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={onNodeClick}
      onPaneClick={onPaneClick}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      minZoom={0.2}
      maxZoom={2}
      proOptions={{ hideAttribution: true }}
      style={{ background: '#0f1117' }}
    >
      <Background
        variant={BackgroundVariant.Dots}
        gap={20}
        size={1}
        color="#2e3347"
      />
      <Controls
        style={{
          background: '#1a1d27',
          border: '1px solid #2e3347',
          borderRadius: 8,
        }}
      />
      <MiniMap
        nodeColor={(node) => {
          const taskNode = node as TaskFlowNode
          return (
            STATUS_COLORS[
              taskNode.data.task.status as keyof typeof STATUS_COLORS
            ] ?? '#4b5563'
          )
        }}
        maskColor="rgba(15,17,23,0.8)"
        style={{
          background: '#1a1d27',
          border: '1px solid #2e3347',
          borderRadius: 8,
        }}
      />
    </ReactFlow>
  )
}

// ─── Public component (wraps with ReactFlowProvider) ─────────────────────────

export function WorkflowGraph() {
  return (
    <ReactFlowProvider>
      <WorkflowGraphInner />
    </ReactFlowProvider>
  )
}
