/**
 * workflowStore.test.ts — Unit tests for the FlowOS Zustand workflow store
 *
 * Tests cover:
 * - Initial state shape
 * - setWorkflows / setWorkflowsLoading / setWorkflowsError
 * - selectWorkflow: sets selectedWorkflow, clears tasks/checkpoints/handoffs
 * - updateWorkflow: updates in-place, updates selectedWorkflow if selected
 * - upsertWorkflow: inserts new, updates existing
 * - setTasks / upsertTask
 * - setCheckpoints / upsertCheckpoint / selectCheckpoint
 * - setHandoffs / upsertHandoff / selectHandoff
 * - pushEvent: ring buffer capped at maxEvents
 * - clearEvents
 * - setWsStatus
 * - selectTask / setEventFilter / setTaskFilter
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { useWorkflowStore } from './workflowStore'
import type { Workflow, Task, Checkpoint, Handoff, FlowEvent } from './workflowStore'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeWorkflow(overrides: Partial<Workflow> = {}): Workflow {
  return {
    workflow_id: 'wf-' + Math.random().toString(36).slice(2),
    name: 'test-workflow',
    status: 'pending',
    trigger: 'api',
    definition: null,
    temporal_run_id: null,
    temporal_workflow_id: null,
    owner_agent_id: null,
    project: null,
    inputs: {},
    outputs: {},
    error_message: null,
    error_details: {},
    tags: [],
    created_at: '2026-01-01T00:00:00Z',
    started_at: null,
    completed_at: null,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeTask(workflowId: string, overrides: Partial<Task> = {}): Task {
  return {
    task_id: 'task-' + Math.random().toString(36).slice(2),
    workflow_id: workflowId,
    step_id: null,
    name: 'test-task',
    description: null,
    task_type: 'human',
    status: 'pending',
    priority: 'normal',
    assigned_agent_id: null,
    previous_agent_id: null,
    workspace_id: null,
    inputs: [],
    outputs: [],
    depends_on: [],
    timeout_secs: 0,
    retry_count: 0,
    current_retry: 0,
    error_message: null,
    error_details: {},
    tags: [],
    task_metadata: {},
    created_at: '2026-01-01T00:00:00Z',
    assigned_at: null,
    started_at: null,
    completed_at: null,
    updated_at: '2026-01-01T00:00:00Z',
    due_at: null,
    ...overrides,
  }
}

function makeCheckpoint(workflowId: string, overrides: Partial<Checkpoint> = {}): Checkpoint {
  return {
    checkpoint_id: 'cp-' + Math.random().toString(36).slice(2),
    task_id: 'task-1',
    workflow_id: workflowId,
    agent_id: 'agent-1',
    workspace_id: 'ws-1',
    checkpoint_type: 'manual',
    status: 'committed',
    sequence: 1,
    git_commit_sha: 'abc123',
    git_branch: 'main',
    git_tag: null,
    message: 'Test checkpoint',
    file_count: 3,
    lines_added: 10,
    lines_removed: 2,
    s3_archive_key: null,
    task_progress: null,
    notes: null,
    checkpoint_metadata: {},
    created_at: '2026-01-01T00:00:00Z',
    verified_at: null,
    reverted_at: null,
    ...overrides,
  }
}

function makeHandoff(workflowId: string, overrides: Partial<Handoff> = {}): Handoff {
  return {
    handoff_id: 'ho-' + Math.random().toString(36).slice(2),
    task_id: 'task-1',
    workflow_id: workflowId,
    source_agent_id: 'agent-1',
    target_agent_id: 'agent-2',
    handoff_type: 'ai_to_human',
    status: 'pending',
    checkpoint_id: null,
    workspace_id: null,
    context: null,
    rejection_reason: null,
    failure_reason: null,
    priority: 'normal',
    expires_at: null,
    accepted_at: null,
    completed_at: null,
    tags: [],
    handoff_metadata: {},
    requested_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeEvent(overrides: Partial<FlowEvent> = {}): FlowEvent {
  return {
    event_id: 'evt-' + Math.random().toString(36).slice(2),
    event_type: 'WORKFLOW_CREATED',
    topic: 'flowos.workflow.events',
    source: 'api',
    workflow_id: null,
    task_id: null,
    agent_id: null,
    correlation_id: null,
    causation_id: null,
    severity: 'info',
    payload: {},
    metadata: {},
    occurred_at: '2026-01-01T00:00:00Z',
    schema_version: '1.0',
    ...overrides,
  }
}

// Reset store before each test
beforeEach(() => {
  useWorkflowStore.setState({
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
  })
})

// ---------------------------------------------------------------------------
// Initial state
// ---------------------------------------------------------------------------

describe('initial state', () => {
  it('starts with empty workflows array', () => {
    const { workflows } = useWorkflowStore.getState()
    expect(workflows).toEqual([])
  })

  it('starts with wsStatus disconnected', () => {
    const { wsStatus } = useWorkflowStore.getState()
    expect(wsStatus).toBe('disconnected')
  })

  it('starts with no selected workflow', () => {
    const { selectedWorkflowId, selectedWorkflow } = useWorkflowStore.getState()
    expect(selectedWorkflowId).toBeNull()
    expect(selectedWorkflow).toBeNull()
  })

  it('starts with maxEvents of 500', () => {
    const { maxEvents } = useWorkflowStore.getState()
    expect(maxEvents).toBe(500)
  })
})

// ---------------------------------------------------------------------------
// Workflow actions
// ---------------------------------------------------------------------------

describe('setWorkflows', () => {
  it('replaces the workflows array and sets total', () => {
    const wf1 = makeWorkflow({ name: 'workflow-1' })
    const wf2 = makeWorkflow({ name: 'workflow-2' })

    useWorkflowStore.getState().setWorkflows([wf1, wf2], 2)

    const { workflows, workflowsTotal } = useWorkflowStore.getState()
    expect(workflows).toHaveLength(2)
    expect(workflowsTotal).toBe(2)
    expect(workflows[0].name).toBe('workflow-1')
    expect(workflows[1].name).toBe('workflow-2')
  })

  it('can set an empty list', () => {
    useWorkflowStore.getState().setWorkflows([], 0)

    const { workflows, workflowsTotal } = useWorkflowStore.getState()
    expect(workflows).toHaveLength(0)
    expect(workflowsTotal).toBe(0)
  })
})

describe('setWorkflowsLoading', () => {
  it('sets loading to true', () => {
    useWorkflowStore.getState().setWorkflowsLoading(true)
    expect(useWorkflowStore.getState().workflowsLoading).toBe(true)
  })

  it('sets loading to false', () => {
    useWorkflowStore.getState().setWorkflowsLoading(true)
    useWorkflowStore.getState().setWorkflowsLoading(false)
    expect(useWorkflowStore.getState().workflowsLoading).toBe(false)
  })
})

describe('setWorkflowsError', () => {
  it('sets an error message', () => {
    useWorkflowStore.getState().setWorkflowsError('Failed to load workflows')
    expect(useWorkflowStore.getState().workflowsError).toBe('Failed to load workflows')
  })

  it('clears the error', () => {
    useWorkflowStore.getState().setWorkflowsError('some error')
    useWorkflowStore.getState().setWorkflowsError(null)
    expect(useWorkflowStore.getState().workflowsError).toBeNull()
  })
})

describe('selectWorkflow', () => {
  it('sets selectedWorkflowId and selectedWorkflow when workflow exists', () => {
    const wf = makeWorkflow({ workflow_id: 'wf-123', name: 'my-workflow' })
    useWorkflowStore.getState().setWorkflows([wf], 1)

    useWorkflowStore.getState().selectWorkflow('wf-123')

    const { selectedWorkflowId, selectedWorkflow } = useWorkflowStore.getState()
    expect(selectedWorkflowId).toBe('wf-123')
    expect(selectedWorkflow).not.toBeNull()
    expect(selectedWorkflow!.name).toBe('my-workflow')
  })

  it('clears tasks, checkpoints, handoffs when selecting a workflow', () => {
    const wf = makeWorkflow({ workflow_id: 'wf-123' })
    useWorkflowStore.getState().setWorkflows([wf], 1)
    useWorkflowStore.getState().setTasks([makeTask('wf-123')])
    useWorkflowStore.getState().setCheckpoints([makeCheckpoint('wf-123')], 1)
    useWorkflowStore.getState().setHandoffs([makeHandoff('wf-123')], 1)

    useWorkflowStore.getState().selectWorkflow('wf-123')

    const { tasks, checkpoints, handoffs } = useWorkflowStore.getState()
    expect(tasks).toHaveLength(0)
    expect(checkpoints).toHaveLength(0)
    expect(handoffs).toHaveLength(0)
  })

  it('sets selectedWorkflow to null when workflowId is null', () => {
    const wf = makeWorkflow({ workflow_id: 'wf-123' })
    useWorkflowStore.getState().setWorkflows([wf], 1)
    useWorkflowStore.getState().selectWorkflow('wf-123')

    useWorkflowStore.getState().selectWorkflow(null)

    const { selectedWorkflowId, selectedWorkflow } = useWorkflowStore.getState()
    expect(selectedWorkflowId).toBeNull()
    expect(selectedWorkflow).toBeNull()
  })

  it('sets selectedWorkflow to null when workflowId does not exist', () => {
    useWorkflowStore.getState().selectWorkflow('nonexistent-id')

    const { selectedWorkflow } = useWorkflowStore.getState()
    expect(selectedWorkflow).toBeNull()
  })
})

describe('updateWorkflow', () => {
  it('updates a workflow in the list by workflow_id', () => {
    const wf = makeWorkflow({ workflow_id: 'wf-123', status: 'pending' })
    useWorkflowStore.getState().setWorkflows([wf], 1)

    const updated = { ...wf, status: 'running' as const }
    useWorkflowStore.getState().updateWorkflow(updated)

    const { workflows } = useWorkflowStore.getState()
    expect(workflows[0].status).toBe('running')
  })

  it('updates selectedWorkflow when the updated workflow is selected', () => {
    const wf = makeWorkflow({ workflow_id: 'wf-123', status: 'pending' })
    useWorkflowStore.getState().setWorkflows([wf], 1)
    useWorkflowStore.getState().selectWorkflow('wf-123')

    const updated = { ...wf, status: 'running' as const }
    useWorkflowStore.getState().updateWorkflow(updated)

    const { selectedWorkflow } = useWorkflowStore.getState()
    expect(selectedWorkflow!.status).toBe('running')
  })

  it('does not change selectedWorkflow when a different workflow is updated', () => {
    const wf1 = makeWorkflow({ workflow_id: 'wf-1', status: 'pending' })
    const wf2 = makeWorkflow({ workflow_id: 'wf-2', status: 'pending' })
    useWorkflowStore.getState().setWorkflows([wf1, wf2], 2)
    useWorkflowStore.getState().selectWorkflow('wf-1')

    const updated = { ...wf2, status: 'running' as const }
    useWorkflowStore.getState().updateWorkflow(updated)

    const { selectedWorkflow } = useWorkflowStore.getState()
    expect(selectedWorkflow!.workflow_id).toBe('wf-1')
    expect(selectedWorkflow!.status).toBe('pending')
  })
})

describe('upsertWorkflow', () => {
  it('inserts a new workflow at the front of the list', () => {
    const wf1 = makeWorkflow({ workflow_id: 'wf-1', name: 'first' })
    useWorkflowStore.getState().setWorkflows([wf1], 1)

    const wf2 = makeWorkflow({ workflow_id: 'wf-2', name: 'second' })
    useWorkflowStore.getState().upsertWorkflow(wf2)

    const { workflows } = useWorkflowStore.getState()
    expect(workflows).toHaveLength(2)
    expect(workflows[0].workflow_id).toBe('wf-2')
    expect(workflows[1].workflow_id).toBe('wf-1')
  })

  it('updates an existing workflow in-place', () => {
    const wf = makeWorkflow({ workflow_id: 'wf-1', status: 'pending' })
    useWorkflowStore.getState().setWorkflows([wf], 1)

    const updated = { ...wf, status: 'completed' as const }
    useWorkflowStore.getState().upsertWorkflow(updated)

    const { workflows } = useWorkflowStore.getState()
    expect(workflows).toHaveLength(1)
    expect(workflows[0].status).toBe('completed')
  })
})

// ---------------------------------------------------------------------------
// Task actions
// ---------------------------------------------------------------------------

describe('setTasks', () => {
  it('replaces the tasks array', () => {
    const task1 = makeTask('wf-1', { name: 'task-1' })
    const task2 = makeTask('wf-1', { name: 'task-2' })

    useWorkflowStore.getState().setTasks([task1, task2])

    const { tasks } = useWorkflowStore.getState()
    expect(tasks).toHaveLength(2)
    expect(tasks[0].name).toBe('task-1')
  })
})

describe('upsertTask', () => {
  it('appends a new task', () => {
    const task = makeTask('wf-1', { task_id: 'task-1' })
    useWorkflowStore.getState().upsertTask(task)

    expect(useWorkflowStore.getState().tasks).toHaveLength(1)
  })

  it('updates an existing task by task_id', () => {
    const task = makeTask('wf-1', { task_id: 'task-1', status: 'pending' })
    useWorkflowStore.getState().setTasks([task])

    const updated = { ...task, status: 'completed' as const }
    useWorkflowStore.getState().upsertTask(updated)

    const { tasks } = useWorkflowStore.getState()
    expect(tasks).toHaveLength(1)
    expect(tasks[0].status).toBe('completed')
  })
})

// ---------------------------------------------------------------------------
// Checkpoint actions
// ---------------------------------------------------------------------------

describe('setCheckpoints', () => {
  it('replaces checkpoints and sets total', () => {
    const cp = makeCheckpoint('wf-1')
    useWorkflowStore.getState().setCheckpoints([cp], 1)

    const { checkpoints, checkpointsTotal } = useWorkflowStore.getState()
    expect(checkpoints).toHaveLength(1)
    expect(checkpointsTotal).toBe(1)
  })
})

describe('upsertCheckpoint', () => {
  it('prepends a new checkpoint', () => {
    const cp1 = makeCheckpoint('wf-1', { checkpoint_id: 'cp-1' })
    useWorkflowStore.getState().setCheckpoints([cp1], 1)

    const cp2 = makeCheckpoint('wf-1', { checkpoint_id: 'cp-2' })
    useWorkflowStore.getState().upsertCheckpoint(cp2)

    const { checkpoints } = useWorkflowStore.getState()
    expect(checkpoints).toHaveLength(2)
    expect(checkpoints[0].checkpoint_id).toBe('cp-2')
  })

  it('updates an existing checkpoint', () => {
    const cp = makeCheckpoint('wf-1', { checkpoint_id: 'cp-1', status: 'committed' })
    useWorkflowStore.getState().setCheckpoints([cp], 1)

    const updated = { ...cp, status: 'verified' as const }
    useWorkflowStore.getState().upsertCheckpoint(updated)

    const { checkpoints } = useWorkflowStore.getState()
    expect(checkpoints).toHaveLength(1)
    expect(checkpoints[0].status).toBe('verified')
  })
})

describe('selectCheckpoint', () => {
  it('sets selectedCheckpointId', () => {
    useWorkflowStore.getState().selectCheckpoint('cp-123')
    expect(useWorkflowStore.getState().selectedCheckpointId).toBe('cp-123')
  })

  it('clears selectedCheckpointId', () => {
    useWorkflowStore.getState().selectCheckpoint('cp-123')
    useWorkflowStore.getState().selectCheckpoint(null)
    expect(useWorkflowStore.getState().selectedCheckpointId).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Handoff actions
// ---------------------------------------------------------------------------

describe('setHandoffs', () => {
  it('replaces handoffs and sets total', () => {
    const ho = makeHandoff('wf-1')
    useWorkflowStore.getState().setHandoffs([ho], 1)

    const { handoffs, handoffsTotal } = useWorkflowStore.getState()
    expect(handoffs).toHaveLength(1)
    expect(handoffsTotal).toBe(1)
  })
})

describe('upsertHandoff', () => {
  it('appends a new handoff', () => {
    const ho = makeHandoff('wf-1', { handoff_id: 'ho-1' })
    useWorkflowStore.getState().upsertHandoff(ho)

    expect(useWorkflowStore.getState().handoffs).toHaveLength(1)
  })

  it('updates an existing handoff', () => {
    const ho = makeHandoff('wf-1', { handoff_id: 'ho-1', status: 'pending' })
    useWorkflowStore.getState().setHandoffs([ho], 1)

    const updated = { ...ho, status: 'accepted' as const }
    useWorkflowStore.getState().upsertHandoff(updated)

    const { handoffs } = useWorkflowStore.getState()
    expect(handoffs).toHaveLength(1)
    expect(handoffs[0].status).toBe('accepted')
  })
})

// ---------------------------------------------------------------------------
// Event feed (ring buffer)
// ---------------------------------------------------------------------------

describe('pushEvent', () => {
  it('adds an event to the front of the events array', () => {
    const evt = makeEvent({ event_type: 'WORKFLOW_CREATED' })
    useWorkflowStore.getState().pushEvent(evt)

    const { events } = useWorkflowStore.getState()
    expect(events).toHaveLength(1)
    expect(events[0].event_type).toBe('WORKFLOW_CREATED')
  })

  it('keeps newest events first (prepend)', () => {
    const evt1 = makeEvent({ event_type: 'WORKFLOW_CREATED' })
    const evt2 = makeEvent({ event_type: 'TASK_ASSIGNED' })

    useWorkflowStore.getState().pushEvent(evt1)
    useWorkflowStore.getState().pushEvent(evt2)

    const { events } = useWorkflowStore.getState()
    expect(events[0].event_type).toBe('TASK_ASSIGNED')
    expect(events[1].event_type).toBe('WORKFLOW_CREATED')
  })

  it('caps the ring buffer at maxEvents', () => {
    // Set a small maxEvents for testing
    useWorkflowStore.setState({ maxEvents: 5 })

    for (let i = 0; i < 7; i++) {
      useWorkflowStore.getState().pushEvent(makeEvent({ event_type: `EVENT_${i}` }))
    }

    const { events } = useWorkflowStore.getState()
    expect(events).toHaveLength(5)
    // Newest events should be kept
    expect(events[0].event_type).toBe('EVENT_6')
    expect(events[4].event_type).toBe('EVENT_2')
  })
})

describe('clearEvents', () => {
  it('empties the events array', () => {
    useWorkflowStore.getState().pushEvent(makeEvent())
    useWorkflowStore.getState().pushEvent(makeEvent())

    useWorkflowStore.getState().clearEvents()

    expect(useWorkflowStore.getState().events).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// WebSocket status
// ---------------------------------------------------------------------------

describe('setWsStatus', () => {
  it('sets wsStatus to connected', () => {
    useWorkflowStore.getState().setWsStatus('connected')
    expect(useWorkflowStore.getState().wsStatus).toBe('connected')
  })

  it('sets wsStatus to error', () => {
    useWorkflowStore.getState().setWsStatus('error')
    expect(useWorkflowStore.getState().wsStatus).toBe('error')
  })

  it('transitions through all valid states', () => {
    const states = ['connecting', 'connected', 'disconnected', 'error'] as const
    for (const s of states) {
      useWorkflowStore.getState().setWsStatus(s)
      expect(useWorkflowStore.getState().wsStatus).toBe(s)
    }
  })
})

// ---------------------------------------------------------------------------
// UI actions
// ---------------------------------------------------------------------------

describe('selectTask', () => {
  it('sets selectedTaskId', () => {
    useWorkflowStore.getState().selectTask('task-123')
    expect(useWorkflowStore.getState().selectedTaskId).toBe('task-123')
  })

  it('clears selectedTaskId', () => {
    useWorkflowStore.getState().selectTask('task-123')
    useWorkflowStore.getState().selectTask(null)
    expect(useWorkflowStore.getState().selectedTaskId).toBeNull()
  })
})

describe('setEventFilter', () => {
  it('sets the event filter', () => {
    useWorkflowStore.getState().setEventFilter('WORKFLOW_')
    expect(useWorkflowStore.getState().eventFilter).toBe('WORKFLOW_')
  })

  it('clears the event filter', () => {
    useWorkflowStore.getState().setEventFilter('WORKFLOW_')
    useWorkflowStore.getState().setEventFilter(null)
    expect(useWorkflowStore.getState().eventFilter).toBeNull()
  })
})

describe('setTaskFilter', () => {
  it('sets the task filter', () => {
    useWorkflowStore.getState().setTaskFilter('running')
    expect(useWorkflowStore.getState().taskFilter).toBe('running')
  })

  it('clears the task filter', () => {
    useWorkflowStore.getState().setTaskFilter('running')
    useWorkflowStore.getState().setTaskFilter(null)
    expect(useWorkflowStore.getState().taskFilter).toBeNull()
  })
})
