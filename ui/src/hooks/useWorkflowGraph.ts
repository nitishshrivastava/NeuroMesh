/**
 * useWorkflowGraph.ts — FlowOS Workflow Graph Data Hook
 *
 * Derives React Flow nodes and edges from the selected workflow's tasks.
 * Also polls the API to keep workflow + task data fresh.
 *
 * Node layout:
 *   - Tasks are laid out in a simple left-to-right DAG using dependency
 *     information from task.depends_on.
 *   - Nodes are coloured by task status.
 *   - Edges connect dependency relationships.
 *
 * Polling:
 *   - Polls every POLL_INTERVAL_MS when a workflow is selected and running.
 *   - Stops polling when the workflow reaches a terminal state.
 */

import { useEffect, useCallback, useMemo, useRef } from 'react'
import type { Node, Edge } from '@xyflow/react'
import {
  useWorkflowStore,
  type Task,
  type TaskStatus,
} from '../store/workflowStore'
import {
  listWorkflowTasks,
  getWorkflow,
  normaliseError,
} from '../api/client'

// ─── Constants ────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 3_000
const TERMINAL_STATUSES: Set<string> = new Set([
  'completed',
  'failed',
  'cancelled',
])

// ─── Node / Edge types ────────────────────────────────────────────────────────

export type TaskNodeData = {
  task: Task
  label: string
}

export type FlowNode = Node<TaskNodeData>
export type FlowEdge = Edge

// ─── Status → colour mapping ──────────────────────────────────────────────────

export const STATUS_COLORS: Record<TaskStatus, string> = {
  pending: '#4b5563',    // gray-600
  assigned: '#2563eb',   // blue-600
  accepted: '#7c3aed',   // violet-600
  running: '#d97706',    // amber-600
  completed: '#16a34a',  // green-600
  failed: '#dc2626',     // red-600
  cancelled: '#6b7280',  // gray-500
  skipped: '#9ca3af',    // gray-400
}

export const STATUS_BG: Record<TaskStatus, string> = {
  pending: '#1f2937',
  assigned: '#1e3a5f',
  accepted: '#2e1065',
  running: '#451a03',
  completed: '#052e16',
  failed: '#450a0a',
  cancelled: '#1f2937',
  skipped: '#111827',
}

// ─── Layout helpers ───────────────────────────────────────────────────────────

const NODE_WIDTH = 200
const NODE_HEIGHT = 80
const H_GAP = 80
const V_GAP = 60

/**
 * Assigns x/y positions to tasks using a simple topological-sort-based
 * column layout. Tasks with no dependencies go in column 0; each task's
 * column is max(dependency columns) + 1.
 */
function layoutTasks(tasks: Task[]): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>()
  const taskMap = new Map(tasks.map((t) => [t.task_id, t]))

  // Compute column (depth) for each task
  const columns = new Map<string, number>()
  const getColumn = (taskId: string, visited = new Set<string>()): number => {
    if (columns.has(taskId)) return columns.get(taskId)!
    if (visited.has(taskId)) return 0 // cycle guard
    visited.add(taskId)

    const task = taskMap.get(taskId)
    if (!task || task.depends_on.length === 0) {
      columns.set(taskId, 0)
      return 0
    }

    const maxDepCol = Math.max(
      ...task.depends_on.map((depId) => getColumn(depId, new Set(visited)))
    )
    const col = maxDepCol + 1
    columns.set(taskId, col)
    return col
  }

  tasks.forEach((t) => getColumn(t.task_id))

  // Group tasks by column
  const byColumn = new Map<number, Task[]>()
  tasks.forEach((t) => {
    const col = columns.get(t.task_id) ?? 0
    if (!byColumn.has(col)) byColumn.set(col, [])
    byColumn.get(col)!.push(t)
  })

  // Assign positions
  byColumn.forEach((colTasks, col) => {
    const totalHeight =
      colTasks.length * NODE_HEIGHT + (colTasks.length - 1) * V_GAP
    const startY = -totalHeight / 2

    colTasks.forEach((task, rowIdx) => {
      positions.set(task.task_id, {
        x: col * (NODE_WIDTH + H_GAP),
        y: startY + rowIdx * (NODE_HEIGHT + V_GAP),
      })
    })
  })

  return positions
}

// ─── Hook ─────────────────────────────────────────────────────────────────────

export interface UseWorkflowGraphResult {
  nodes: FlowNode[]
  edges: FlowEdge[]
  loading: boolean
  error: string | null
  refresh: () => void
}

export function useWorkflowGraph(): UseWorkflowGraphResult {
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)
  const tasks = useWorkflowStore((s) => s.tasks)
  const tasksLoading = useWorkflowStore((s) => s.tasksLoading)
  const tasksError = useWorkflowStore((s) => s.tasksError)

  const setTasks = useWorkflowStore((s) => s.setTasks)
  const setTasksLoading = useWorkflowStore((s) => s.setTasksLoading)
  const setTasksError = useWorkflowStore((s) => s.setTasksError)
  const updateWorkflow = useWorkflowStore((s) => s.updateWorkflow)

  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const workflowIdRef = useRef<string | null>(null)

  const fetchData = useCallback(
    async (workflowId: string) => {
      setTasksLoading(true)
      setTasksError(null)
      try {
        const [tasksRes, workflow] = await Promise.all([
          listWorkflowTasks(workflowId),
          getWorkflow(workflowId),
        ])
        setTasks(tasksRes.items)
        updateWorkflow(workflow)
      } catch (err) {
        const apiErr = normaliseError(err)
        setTasksError(apiErr.message)
      } finally {
        setTasksLoading(false)
      }
    },
    [setTasks, setTasksLoading, setTasksError, updateWorkflow]
  )

  const refresh = useCallback(() => {
    if (selectedWorkflowId) {
      fetchData(selectedWorkflowId)
    }
  }, [selectedWorkflowId, fetchData])

  // Start/stop polling when selected workflow changes
  useEffect(() => {
    // Clear previous polling
    if (pollingRef.current !== null) {
      clearInterval(pollingRef.current)
      pollingRef.current = null
    }

    if (!selectedWorkflowId) {
      workflowIdRef.current = null
      return
    }

    workflowIdRef.current = selectedWorkflowId

    // Initial fetch
    fetchData(selectedWorkflowId)

    // Poll only for non-terminal workflows
    const isTerminal = selectedWorkflow
      ? TERMINAL_STATUSES.has(selectedWorkflow.status)
      : false

    if (!isTerminal) {
      pollingRef.current = setInterval(() => {
        if (workflowIdRef.current) {
          fetchData(workflowIdRef.current)
        }
      }, POLL_INTERVAL_MS)
    }

    return () => {
      if (pollingRef.current !== null) {
        clearInterval(pollingRef.current)
        pollingRef.current = null
      }
    }
  }, [selectedWorkflowId, selectedWorkflow?.status, fetchData])

  // Stop polling when workflow reaches terminal state
  useEffect(() => {
    if (
      selectedWorkflow &&
      TERMINAL_STATUSES.has(selectedWorkflow.status) &&
      pollingRef.current !== null
    ) {
      clearInterval(pollingRef.current)
      pollingRef.current = null
    }
  }, [selectedWorkflow?.status])

  // Derive React Flow nodes and edges from tasks
  const { nodes, edges } = useMemo<{
    nodes: FlowNode[]
    edges: FlowEdge[]
  }>(() => {
    if (tasks.length === 0) return { nodes: [], edges: [] }

    const positions = layoutTasks(tasks)

    const nodes: FlowNode[] = tasks.map((task) => {
      const pos = positions.get(task.task_id) ?? { x: 0, y: 0 }
      return {
        id: task.task_id,
        type: 'taskNode',
        position: pos,
        data: {
          task,
          label: task.name,
        },
        style: {
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
        },
      }
    })

    const edges: FlowEdge[] = []
    tasks.forEach((task) => {
      task.depends_on.forEach((depId) => {
        edges.push({
          id: `${depId}->${task.task_id}`,
          source: depId,
          target: task.task_id,
          type: 'smoothstep',
          animated: task.status === 'running',
          style: {
            stroke:
              task.status === 'running'
                ? STATUS_COLORS.running
                : task.status === 'completed'
                ? STATUS_COLORS.completed
                : '#4b5563',
            strokeWidth: 2,
          },
        })
      })
    })

    return { nodes, edges }
  }, [tasks])

  return {
    nodes,
    edges,
    loading: tasksLoading,
    error: tasksError,
    refresh,
  }
}
