/**
 * workflowStore.ts — FlowOS Zustand Global State Store
 *
 * Central state management for the FlowOS UI. Holds:
 *   - workflows list + selected workflow
 *   - tasks for the selected workflow
 *   - live event feed (ring buffer, newest-first)
 *   - WebSocket connection status
 *   - UI state (selected task, panel visibility)
 *
 * All mutations happen through actions; components subscribe to slices.
 */

import { create } from 'zustand'
import { subscribeWithSelector } from 'zustand/middleware'

// ─── Domain Types ─────────────────────────────────────────────────────────────

export type WorkflowStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface Workflow {
  workflow_id: string
  name: string
  status: WorkflowStatus
  trigger: string
  definition: Record<string, unknown> | null
  temporal_run_id: string | null
  temporal_workflow_id: string | null
  owner_agent_id: string | null
  project: string | null
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  error_message: string | null
  error_details: Record<string, unknown>
  tags: string[]
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
}

export type TaskStatus =
  | 'pending'
  | 'assigned'
  | 'accepted'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'skipped'

export interface Task {
  task_id: string
  workflow_id: string
  step_id: string | null
  name: string
  description: string | null
  task_type: string
  status: TaskStatus
  priority: string
  assigned_agent_id: string | null
  previous_agent_id: string | null
  workspace_id: string | null
  inputs: Array<Record<string, unknown>>
  outputs: Array<Record<string, unknown>>
  depends_on: string[]
  timeout_secs: number
  retry_count: number
  current_retry: number
  error_message: string | null
  error_details: Record<string, unknown>
  tags: string[]
  task_metadata: Record<string, string>
  created_at: string
  assigned_at: string | null
  started_at: string | null
  completed_at: string | null
  updated_at: string
  due_at: string | null
}

export interface FlowEvent {
  event_id: string
  event_type: string
  topic: string
  source: string
  workflow_id: string | null
  task_id: string | null
  agent_id: string | null
  correlation_id: string | null
  causation_id: string | null
  severity: string
  payload: Record<string, unknown>
  metadata: Record<string, string>
  occurred_at: string
  schema_version: string
}

export type WsStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

// ─── Store Shape ──────────────────────────────────────────────────────────────

export interface WorkflowState {
  // ── Data ──────────────────────────────────────────────────────────────────
  workflows: Workflow[]
  workflowsTotal: number
  workflowsLoading: boolean
  workflowsError: string | null

  selectedWorkflowId: string | null
  selectedWorkflow: Workflow | null

  tasks: Task[]
  tasksLoading: boolean
  tasksError: string | null

  events: FlowEvent[]
  /** Max events kept in the ring buffer */
  maxEvents: number

  wsStatus: WsStatus

  // ── UI ────────────────────────────────────────────────────────────────────
  selectedTaskId: string | null
  eventFilter: string | null   // filter by event_type prefix
  taskFilter: TaskStatus | null

  // ── Actions ───────────────────────────────────────────────────────────────
  setWorkflows: (workflows: Workflow[], total: number) => void
  setWorkflowsLoading: (loading: boolean) => void
  setWorkflowsError: (error: string | null) => void

  selectWorkflow: (workflowId: string | null) => void
  updateWorkflow: (workflow: Workflow) => void
  upsertWorkflow: (workflow: Workflow) => void

  setTasks: (tasks: Task[]) => void
  setTasksLoading: (loading: boolean) => void
  setTasksError: (error: string | null) => void
  upsertTask: (task: Task) => void

  pushEvent: (event: FlowEvent) => void
  clearEvents: () => void

  setWsStatus: (status: WsStatus) => void

  selectTask: (taskId: string | null) => void
  setEventFilter: (filter: string | null) => void
  setTaskFilter: (filter: TaskStatus | null) => void
}

// ─── Store Implementation ─────────────────────────────────────────────────────

export const useWorkflowStore = create<WorkflowState>()(
  subscribeWithSelector((set, get) => ({
    // ── Initial state ────────────────────────────────────────────────────────
    workflows: [],
    workflowsTotal: 0,
    workflowsLoading: false,
    workflowsError: null,

    selectedWorkflowId: null,
    selectedWorkflow: null,

    tasks: [],
    tasksLoading: false,
    tasksError: null,

    events: [],
    maxEvents: 500,

    wsStatus: 'disconnected',

    selectedTaskId: null,
    eventFilter: null,
    taskFilter: null,

    // ── Workflow actions ─────────────────────────────────────────────────────
    setWorkflows: (workflows, total) =>
      set({ workflows, workflowsTotal: total }),

    setWorkflowsLoading: (loading) =>
      set({ workflowsLoading: loading }),

    setWorkflowsError: (error) =>
      set({ workflowsError: error }),

    selectWorkflow: (workflowId) => {
      const { workflows } = get()
      const found = workflowId
        ? (workflows.find((w) => w.workflow_id === workflowId) ?? null)
        : null
      set({
        selectedWorkflowId: workflowId,
        selectedWorkflow: found,
        selectedTaskId: null,
        tasks: [],
      })
    },

    updateWorkflow: (workflow) =>
      set((state) => ({
        workflows: state.workflows.map((w) =>
          w.workflow_id === workflow.workflow_id ? workflow : w
        ),
        selectedWorkflow:
          state.selectedWorkflowId === workflow.workflow_id
            ? workflow
            : state.selectedWorkflow,
      })),

    upsertWorkflow: (workflow) =>
      set((state) => {
        const exists = state.workflows.some(
          (w) => w.workflow_id === workflow.workflow_id
        )
        const workflows = exists
          ? state.workflows.map((w) =>
              w.workflow_id === workflow.workflow_id ? workflow : w
            )
          : [workflow, ...state.workflows]
        return {
          workflows,
          selectedWorkflow:
            state.selectedWorkflowId === workflow.workflow_id
              ? workflow
              : state.selectedWorkflow,
        }
      }),

    // ── Task actions ─────────────────────────────────────────────────────────
    setTasks: (tasks) => set({ tasks }),
    setTasksLoading: (loading) => set({ tasksLoading: loading }),
    setTasksError: (error) => set({ tasksError: error }),

    upsertTask: (task) =>
      set((state) => {
        const exists = state.tasks.some((t) => t.task_id === task.task_id)
        const tasks = exists
          ? state.tasks.map((t) => (t.task_id === task.task_id ? task : t))
          : [...state.tasks, task]
        return { tasks }
      }),

    // ── Event actions ────────────────────────────────────────────────────────
    pushEvent: (event) =>
      set((state) => {
        const events = [event, ...state.events].slice(0, state.maxEvents)
        return { events }
      }),

    clearEvents: () => set({ events: [] }),

    // ── WebSocket status ─────────────────────────────────────────────────────
    setWsStatus: (status) => set({ wsStatus: status }),

    // ── UI actions ───────────────────────────────────────────────────────────
    selectTask: (taskId) => set({ selectedTaskId: taskId }),
    setEventFilter: (filter) => set({ eventFilter: filter }),
    setTaskFilter: (filter) => set({ taskFilter: filter }),
  }))
)

// ─── Derived selectors ────────────────────────────────────────────────────────

/** Returns tasks filtered by the current taskFilter */
export const selectFilteredTasks = (state: WorkflowState): Task[] => {
  if (!state.taskFilter) return state.tasks
  return state.tasks.filter((t) => t.status === state.taskFilter)
}

/** Returns events filtered by the current eventFilter */
export const selectFilteredEvents = (state: WorkflowState): FlowEvent[] => {
  if (!state.eventFilter) return state.events
  return state.events.filter((e) =>
    e.event_type.startsWith(state.eventFilter!)
  )
}

/** Returns events scoped to the selected workflow */
export const selectWorkflowEvents = (state: WorkflowState): FlowEvent[] => {
  if (!state.selectedWorkflowId) return state.events
  return state.events.filter(
    (e) => e.workflow_id === state.selectedWorkflowId
  )
}

/** Returns the currently selected task object */
export const selectSelectedTask = (state: WorkflowState): Task | null => {
  if (!state.selectedTaskId) return null
  return state.tasks.find((t) => t.task_id === state.selectedTaskId) ?? null
}
