/**
 * workflowStore.ts — FlowOS Zustand Global State Store
 *
 * Central state management for the FlowOS UI. Holds:
 *   - workflows list + selected workflow
 *   - tasks for the selected workflow
 *   - checkpoints for the selected workflow
 *   - handoffs for the selected workflow
 *   - agents registry
 *   - live event feed (ring buffer, newest-first)
 *   - WebSocket connection status
 *   - UI state (selected task, panel visibility)
 *
 * All mutations happen through actions; components subscribe to slices.
 *
 * TypeScript interfaces for Checkpoint, Handoff, and Agent are defined
 * here so they can be shared across components without duplication.
 */

import { create } from 'zustand'
import { subscribeWithSelector } from 'zustand/middleware'
import { apiClient, normaliseError, type CreateWorkflowBody } from '../api/client'

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

// ─── Checkpoint Types ──────────────────────────────────────────────────────────

export type CheckpointStatus =
  | 'pending'
  | 'committed'
  | 'verified'
  | 'reverted'
  | 'failed'
  | string

export type CheckpointType =
  | 'manual'
  | 'auto'
  | 'pre_handoff'
  | 'post_handoff'
  | 'milestone'
  | 'recovery'
  | string

export interface CheckpointFile {
  file_id: string
  checkpoint_id: string
  path: string
  status: 'added' | 'modified' | 'deleted' | 'renamed' | string
  old_path: string | null
  size_bytes: number | null
  checksum: string | null
}

export interface Checkpoint {
  checkpoint_id: string
  task_id: string
  workflow_id: string
  agent_id: string
  workspace_id: string
  checkpoint_type: CheckpointType
  status: CheckpointStatus
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

// ─── Handoff Types ─────────────────────────────────────────────────────────────

export type HandoffStatus =
  | 'pending'
  | 'accepted'
  | 'completed'
  | 'rejected'
  | 'cancelled'
  | 'failed'
  | string

export interface Handoff {
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

// ─── Agent Types ───────────────────────────────────────────────────────────────

export type AgentStatus =
  | 'online'
  | 'offline'
  | 'idle'
  | 'busy'
  | 'error'
  | string

export interface AgentCapability {
  capability_id: string
  name: string
  version: string | null
  description: string | null
  enabled: boolean
}

export interface Agent {
  agent_id: string
  name: string
  agent_type: string
  status: AgentStatus
  capabilities: string[]
  current_task_id: string | null
  project: string | null
  tags: string[]
  registered_at: string
  last_seen_at: string | null
}

// ─── Event Types ───────────────────────────────────────────────────────────────

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

// ─── Store Shape ───────────────────────────────────────────────────────────────

export interface WorkflowState {
  // ── Workflow data ──────────────────────────────────────────────────────────
  workflows: Workflow[]
  workflowsTotal: number
  workflowsLoading: boolean
  workflowsError: string | null

  selectedWorkflowId: string | null
  selectedWorkflow: Workflow | null

  // ── Task data ──────────────────────────────────────────────────────────────
  tasks: Task[]
  tasksLoading: boolean
  tasksError: string | null

  // ── Checkpoint data ────────────────────────────────────────────────────────
  checkpoints: Checkpoint[]
  checkpointsTotal: number
  checkpointsLoading: boolean
  checkpointsError: string | null

  // ── Handoff data ───────────────────────────────────────────────────────────
  handoffs: Handoff[]
  handoffsTotal: number
  handoffsLoading: boolean
  handoffsError: string | null

  // ── Agent data ─────────────────────────────────────────────────────────────
  agents: Agent[]
  agentsTotal: number
  agentsLoading: boolean
  agentsError: string | null

  // ── Event feed ─────────────────────────────────────────────────────────────
  events: FlowEvent[]
  /** Max events kept in the ring buffer */
  maxEvents: number

  // ── WebSocket ──────────────────────────────────────────────────────────────
  wsStatus: WsStatus

  // ── UI state ───────────────────────────────────────────────────────────────
  selectedTaskId: string | null
  selectedCheckpointId: string | null
  selectedHandoffId: string | null
  eventFilter: string | null   // filter by event_type prefix
  taskFilter: TaskStatus | null

  // ── Workflow actions ───────────────────────────────────────────────────────
  setWorkflows: (workflows: Workflow[], total: number) => void
  setWorkflowsLoading: (loading: boolean) => void
  setWorkflowsError: (error: string | null) => void

  selectWorkflow: (workflowId: string | null) => void
  updateWorkflow: (workflow: Workflow) => void
  upsertWorkflow: (workflow: Workflow) => void

  /**
   * Async action: calls POST /api/workflows, upserts the result into the
   * store, and selects the new workflow. Returns the created Workflow on
   * success or throws a normalised error string on failure.
   */
  createWorkflow: (body: CreateWorkflowBody) => Promise<Workflow>

  // ── Task actions ───────────────────────────────────────────────────────────
  setTasks: (tasks: Task[]) => void
  setTasksLoading: (loading: boolean) => void
  setTasksError: (error: string | null) => void
  upsertTask: (task: Task) => void

  // ── Checkpoint actions ─────────────────────────────────────────────────────
  setCheckpoints: (checkpoints: Checkpoint[], total: number) => void
  setCheckpointsLoading: (loading: boolean) => void
  setCheckpointsError: (error: string | null) => void
  upsertCheckpoint: (checkpoint: Checkpoint) => void
  selectCheckpoint: (checkpointId: string | null) => void

  // ── Handoff actions ────────────────────────────────────────────────────────
  setHandoffs: (handoffs: Handoff[], total: number) => void
  setHandoffsLoading: (loading: boolean) => void
  setHandoffsError: (error: string | null) => void
  upsertHandoff: (handoff: Handoff) => void
  selectHandoff: (handoffId: string | null) => void

  // ── Agent actions ──────────────────────────────────────────────────────────
  setAgents: (agents: Agent[], total: number) => void
  setAgentsLoading: (loading: boolean) => void
  setAgentsError: (error: string | null) => void
  upsertAgent: (agent: Agent) => void

  // ── Event actions ──────────────────────────────────────────────────────────
  pushEvent: (event: FlowEvent) => void
  clearEvents: () => void

  // ── WebSocket status ───────────────────────────────────────────────────────
  setWsStatus: (status: WsStatus) => void

  // ── UI actions ─────────────────────────────────────────────────────────────
  selectTask: (taskId: string | null) => void
  setEventFilter: (filter: string | null) => void
  setTaskFilter: (filter: TaskStatus | null) => void
}

// ─── Store Implementation ──────────────────────────────────────────────────────

export const useWorkflowStore = create<WorkflowState>()(
  subscribeWithSelector((set, get) => ({
    // ── Initial state ──────────────────────────────────────────────────────
    workflows: [],
    workflowsTotal: 0,
    workflowsLoading: false,
    workflowsError: null,

    selectedWorkflowId: null,
    selectedWorkflow: null,

    tasks: [],
    tasksLoading: false,
    tasksError: null,

    checkpoints: [],
    checkpointsTotal: 0,
    checkpointsLoading: false,
    checkpointsError: null,

    handoffs: [],
    handoffsTotal: 0,
    handoffsLoading: false,
    handoffsError: null,

    agents: [],
    agentsTotal: 0,
    agentsLoading: false,
    agentsError: null,

    events: [],
    maxEvents: 500,

    wsStatus: 'disconnected',

    selectedTaskId: null,
    selectedCheckpointId: null,
    selectedHandoffId: null,
    eventFilter: null,
    taskFilter: null,

    // ── Workflow actions ───────────────────────────────────────────────────
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
        selectedCheckpointId: null,
        selectedHandoffId: null,
        tasks: [],
        checkpoints: [],
        handoffs: [],
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

    createWorkflow: async (body) => {
      try {
        const res = await apiClient.post<Workflow>('/workflows', body)
        const workflow = res.data
        // Prepend to the list and select it
        get().upsertWorkflow(workflow)
        get().selectWorkflow(workflow.workflow_id)
        return workflow
      } catch (err) {
        throw new Error(normaliseError(err).message)
      }
    },

    // ── Task actions ───────────────────────────────────────────────────────
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

    // ── Checkpoint actions ─────────────────────────────────────────────────
    setCheckpoints: (checkpoints, total) =>
      set({ checkpoints, checkpointsTotal: total }),

    setCheckpointsLoading: (loading) =>
      set({ checkpointsLoading: loading }),

    setCheckpointsError: (error) =>
      set({ checkpointsError: error }),

    upsertCheckpoint: (checkpoint) =>
      set((state) => {
        const exists = state.checkpoints.some(
          (c) => c.checkpoint_id === checkpoint.checkpoint_id
        )
        const checkpoints = exists
          ? state.checkpoints.map((c) =>
              c.checkpoint_id === checkpoint.checkpoint_id ? checkpoint : c
            )
          : [checkpoint, ...state.checkpoints]
        return { checkpoints }
      }),

    selectCheckpoint: (checkpointId) =>
      set({ selectedCheckpointId: checkpointId }),

    // ── Handoff actions ────────────────────────────────────────────────────
    setHandoffs: (handoffs, total) =>
      set({ handoffs, handoffsTotal: total }),

    setHandoffsLoading: (loading) =>
      set({ handoffsLoading: loading }),

    setHandoffsError: (error) =>
      set({ handoffsError: error }),

    upsertHandoff: (handoff) =>
      set((state) => {
        const exists = state.handoffs.some(
          (h) => h.handoff_id === handoff.handoff_id
        )
        const handoffs = exists
          ? state.handoffs.map((h) =>
              h.handoff_id === handoff.handoff_id ? handoff : h
            )
          : [handoff, ...state.handoffs]
        return { handoffs }
      }),

    selectHandoff: (handoffId) =>
      set({ selectedHandoffId: handoffId }),

    // ── Agent actions ──────────────────────────────────────────────────────
    setAgents: (agents, total) =>
      set({ agents, agentsTotal: total }),

    setAgentsLoading: (loading) =>
      set({ agentsLoading: loading }),

    setAgentsError: (error) =>
      set({ agentsError: error }),

    upsertAgent: (agent) =>
      set((state) => {
        const exists = state.agents.some(
          (a) => a.agent_id === agent.agent_id
        )
        const agents = exists
          ? state.agents.map((a) =>
              a.agent_id === agent.agent_id ? agent : a
            )
          : [...state.agents, agent]
        return { agents }
      }),

    // ── Event actions ──────────────────────────────────────────────────────
    pushEvent: (event) =>
      set((state) => {
        const events = [event, ...state.events].slice(0, state.maxEvents)
        return { events }
      }),

    clearEvents: () => set({ events: [] }),

    // ── WebSocket status ───────────────────────────────────────────────────
    setWsStatus: (status) => set({ wsStatus: status }),

    // ── UI actions ─────────────────────────────────────────────────────────
    selectTask: (taskId) => set({ selectedTaskId: taskId }),
    setEventFilter: (filter) => set({ eventFilter: filter }),
    setTaskFilter: (filter) => set({ taskFilter: filter }),
  }))
)

// ─── Derived selectors ─────────────────────────────────────────────────────────

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

/** Returns the currently selected checkpoint object */
export const selectSelectedCheckpoint = (
  state: WorkflowState
): Checkpoint | null => {
  if (!state.selectedCheckpointId) return null
  return (
    state.checkpoints.find(
      (c) => c.checkpoint_id === state.selectedCheckpointId
    ) ?? null
  )
}

/** Returns the currently selected handoff object */
export const selectSelectedHandoff = (
  state: WorkflowState
): Handoff | null => {
  if (!state.selectedHandoffId) return null
  return (
    state.handoffs.find((h) => h.handoff_id === state.selectedHandoffId) ??
    null
  )
}

/** Returns checkpoints for the selected workflow, sorted by sequence desc */
export const selectWorkflowCheckpoints = (
  state: WorkflowState
): Checkpoint[] => {
  if (!state.selectedWorkflowId) return state.checkpoints
  return state.checkpoints
    .filter((c) => c.workflow_id === state.selectedWorkflowId)
    .sort((a, b) => b.sequence - a.sequence)
}

/** Returns handoffs for the selected workflow, sorted by requested_at desc */
export const selectWorkflowHandoffs = (state: WorkflowState): Handoff[] => {
  if (!state.selectedWorkflowId) return state.handoffs
  return state.handoffs
    .filter((h) => h.workflow_id === state.selectedWorkflowId)
    .sort(
      (a, b) =>
        new Date(b.requested_at).getTime() - new Date(a.requested_at).getTime()
    )
}

/** Returns agents that are currently assigned to a task in the selected workflow */
export const selectWorkflowAgents = (state: WorkflowState): Agent[] => {
  if (!state.selectedWorkflowId) return state.agents
  const agentIds = new Set(
    state.tasks
      .filter(
        (t) =>
          t.workflow_id === state.selectedWorkflowId && t.assigned_agent_id
      )
      .map((t) => t.assigned_agent_id as string)
  )
  if (agentIds.size === 0) return state.agents
  return state.agents.filter((a) => agentIds.has(a.agent_id))
}
