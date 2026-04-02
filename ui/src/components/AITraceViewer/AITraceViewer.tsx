/**
 * AITraceViewer.tsx — FlowOS AI Reasoning Trace Panel
 *
 * Displays the AI reasoning trace for the selected workflow.
 * Shows:
 *   - AI events filtered from the live event feed (AI_* event types)
 *   - Reasoning steps: thoughts, tool calls, observations, proposals
 *   - Token usage and cost estimates
 *   - Human review requests with approve/reject actions
 *   - Expandable payload viewer for each step
 *
 * Data sources:
 *   - Live events from the Zustand store (WebSocket feed)
 *   - Historical AI events from GET /events?event_type=AI_&workflow_id=...
 */

import { useState, useEffect, useCallback, useRef, memo } from 'react'
import { formatDistanceToNow, format } from 'date-fns'
import {
  Brain,
  Wrench,
  Eye,
  Lightbulb,
  CheckCircle2,
  XCircle,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  Loader2,
  Zap,
  MessageSquare,
  Code2,
  GitPullRequest,
  Activity,
} from 'lucide-react'
import {
  useWorkflowStore,
  type FlowEvent,
} from '../../store/workflowStore'
import { apiClient, normaliseError } from '../../api/client'

// ─── Types ────────────────────────────────────────────────────────────────────

type AIEventType =
  | 'AI_TASK_STARTED'
  | 'AI_SUGGESTION_CREATED'
  | 'AI_PATCH_PROPOSED'
  | 'AI_REVIEW_COMPLETED'
  | 'AI_REASONING_TRACE'
  | 'AI_TOOL_CALLED'
  | string

interface AITraceEvent extends FlowEvent {
  event_type: AIEventType
}

interface ReasoningStep {
  step_id?: string
  step_type: string
  sequence?: number
  content: string
  tool_calls?: ToolCall[]
  model_name?: string
  prompt_tokens?: number
  output_tokens?: number
  total_tokens?: number
  confidence?: number
  duration_ms?: number
  created_at?: string
}

interface ToolCall {
  tool_call_id?: string
  tool_name: string
  tool_input?: Record<string, unknown>
  tool_output?: unknown
  error?: string
  duration_ms?: number
  is_error?: boolean
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function isAIEvent(event: FlowEvent): event is AITraceEvent {
  return event.event_type.startsWith('AI_')
}

const STEP_TYPE_COLORS: Record<string, string> = {
  thought: '#6366f1',
  tool_call: '#f59e0b',
  observation: '#22c55e',
  plan: '#3b82f6',
  decision: '#a855f7',
  proposal: '#14b8a6',
  reflection: '#8b5cf6',
  human_input: '#f97316',
  final_answer: '#16a34a',
  error: '#ef4444',
}

const EVENT_TYPE_COLORS: Record<string, string> = {
  AI_TASK_STARTED: '#6366f1',
  AI_SUGGESTION_CREATED: '#22c55e',
  AI_PATCH_PROPOSED: '#14b8a6',
  AI_REVIEW_COMPLETED: '#a855f7',
  AI_REASONING_TRACE: '#f59e0b',
  AI_TOOL_CALLED: '#3b82f6',
}

function getStepTypeColor(type: string): string {
  return STEP_TYPE_COLORS[type] ?? '#6b7280'
}

function getEventTypeColor(type: string): string {
  return EVENT_TYPE_COLORS[type] ?? '#6b7280'
}

function formatTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  try {
    return format(new Date(ts), 'HH:mm:ss.SSS')
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

function StepTypeIcon({ type, size = 13 }: { type: string; size?: number }) {
  const color = getStepTypeColor(type)
  const props = { size, color }
  switch (type) {
    case 'thought':
      return <Brain {...props} />
    case 'tool_call':
      return <Wrench {...props} />
    case 'observation':
      return <Eye {...props} />
    case 'plan':
      return <Lightbulb {...props} />
    case 'decision':
      return <Zap {...props} />
    case 'proposal':
      return <GitPullRequest {...props} />
    case 'reflection':
      return <MessageSquare {...props} />
    case 'final_answer':
      return <CheckCircle2 {...props} />
    case 'error':
      return <XCircle {...props} />
    default:
      return <Activity {...props} />
  }
}

function EventTypeIcon({ type, size = 13 }: { type: string; size?: number }) {
  const color = getEventTypeColor(type)
  const props = { size, color }
  switch (type) {
    case 'AI_TASK_STARTED':
      return <Brain {...props} />
    case 'AI_SUGGESTION_CREATED':
      return <Lightbulb {...props} />
    case 'AI_PATCH_PROPOSED':
      return <Code2 {...props} />
    case 'AI_REVIEW_COMPLETED':
      return <CheckCircle2 {...props} />
    case 'AI_REASONING_TRACE':
      return <Activity {...props} />
    case 'AI_TOOL_CALLED':
      return <Wrench {...props} />
    default:
      return <Brain {...props} />
  }
}

// ─── Tool Call Detail ─────────────────────────────────────────────────────────

function ToolCallDetail({ call }: { call: ToolCall }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div
      style={{
        background: 'rgba(245,158,11,0.06)',
        border: '1px solid rgba(245,158,11,0.2)',
        borderRadius: 4,
        marginTop: 6,
        overflow: 'hidden',
      }}
    >
      <button
        onClick={() => setExpanded((v) => !v)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          width: '100%',
          padding: '6px 8px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        {expanded ? (
          <ChevronDown size={11} color="#f59e0b" />
        ) : (
          <ChevronRight size={11} color="#f59e0b" />
        )}
        <Wrench size={11} color="#f59e0b" />
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: '#fbbf24',
            fontFamily: 'monospace',
          }}
        >
          {call.tool_name}
        </span>
        {call.duration_ms !== undefined && (
          <span style={{ fontSize: 9, color: '#6b7280', marginLeft: 'auto' }}>
            {call.duration_ms}ms
          </span>
        )}
        {call.is_error && (
          <span
            style={{
              fontSize: 9,
              color: '#ef4444',
              background: 'rgba(239,68,68,0.1)',
              border: '1px solid rgba(239,68,68,0.3)',
              borderRadius: 3,
              padding: '1px 4px',
            }}
          >
            ERROR
          </span>
        )}
      </button>
      {expanded && (
        <div style={{ padding: '0 8px 8px' }}>
          {call.tool_input &&
            Object.keys(call.tool_input).length > 0 && (
              <div style={{ marginBottom: 6 }}>
                <div
                  style={{
                    fontSize: 9,
                    fontWeight: 700,
                    color: '#6b7280',
                    textTransform: 'uppercase',
                    letterSpacing: '0.06em',
                    marginBottom: 3,
                  }}
                >
                  Input
                </div>
                <pre
                  style={{
                    margin: 0,
                    fontSize: 10,
                    color: '#cbd5e1',
                    fontFamily: 'monospace',
                    background: 'rgba(0,0,0,0.3)',
                    borderRadius: 3,
                    padding: '6px 8px',
                    overflow: 'auto',
                    maxHeight: 120,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                  }}
                >
                  {JSON.stringify(call.tool_input, null, 2)}
                </pre>
              </div>
            )}
          {call.tool_output !== undefined && call.tool_output !== null && (
            <div style={{ marginBottom: 6 }}>
              <div
                style={{
                  fontSize: 9,
                  fontWeight: 700,
                  color: '#6b7280',
                  textTransform: 'uppercase',
                  letterSpacing: '0.06em',
                  marginBottom: 3,
                }}
              >
                Output
              </div>
              <pre
                style={{
                  margin: 0,
                  fontSize: 10,
                  color: call.is_error ? '#f87171' : '#86efac',
                  fontFamily: 'monospace',
                  background: 'rgba(0,0,0,0.3)',
                  borderRadius: 3,
                  padding: '6px 8px',
                  overflow: 'auto',
                  maxHeight: 120,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                }}
              >
                {typeof call.tool_output === 'string'
                  ? call.tool_output
                  : JSON.stringify(call.tool_output, null, 2)}
              </pre>
            </div>
          )}
          {call.error && (
            <div
              style={{
                fontSize: 10,
                color: '#f87171',
                background: 'rgba(239,68,68,0.08)',
                border: '1px solid rgba(239,68,68,0.2)',
                borderRadius: 3,
                padding: '4px 6px',
              }}
            >
              {call.error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Reasoning Step Card ──────────────────────────────────────────────────────

const ReasoningStepCard = memo(function ReasoningStepCard({
  step,
}: {
  step: ReasoningStep
}) {
  const [expanded, setExpanded] = useState(
    step.step_type === 'final_answer' || step.step_type === 'proposal'
  )
  const color = getStepTypeColor(step.step_type)

  return (
    <div
      style={{
        border: `1px solid ${color}30`,
        borderLeft: `3px solid ${color}`,
        borderRadius: 4,
        marginBottom: 6,
        overflow: 'hidden',
        background: `${color}06`,
      }}
    >
      <button
        onClick={() => setExpanded((v) => !v)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          width: '100%',
          padding: '7px 10px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        {expanded ? (
          <ChevronDown size={11} color={color} />
        ) : (
          <ChevronRight size={11} color={color} />
        )}
        <StepTypeIcon type={step.step_type} size={12} />
        <span
          style={{
            fontSize: 10,
            fontWeight: 700,
            color,
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
          }}
        >
          {step.step_type.replace('_', ' ')}
        </span>
        {step.sequence !== undefined && (
          <span style={{ fontSize: 9, color: '#4b5563' }}>
            #{step.sequence}
          </span>
        )}
        {step.model_name && (
          <span
            style={{
              fontSize: 9,
              color: '#6b7280',
              background: '#1a1d27',
              border: '1px solid #2e3347',
              borderRadius: 3,
              padding: '1px 4px',
              marginLeft: 'auto',
            }}
          >
            {step.model_name}
          </span>
        )}
        {step.total_tokens !== undefined && (
          <span style={{ fontSize: 9, color: '#4b5563' }}>
            {step.total_tokens}t
          </span>
        )}
        {step.duration_ms !== undefined && (
          <span style={{ fontSize: 9, color: '#4b5563' }}>
            {step.duration_ms}ms
          </span>
        )}
      </button>
      {expanded && (
        <div style={{ padding: '0 10px 10px' }}>
          {/* Content */}
          <div
            style={{
              fontSize: 11,
              color: '#cbd5e1',
              lineHeight: 1.6,
              background: 'rgba(0,0,0,0.2)',
              borderRadius: 3,
              padding: '8px 10px',
              marginBottom: step.tool_calls?.length ? 6 : 0,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {step.content}
          </div>

          {/* Confidence */}
          {step.confidence !== undefined && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                marginBottom: 6,
              }}
            >
              <span style={{ fontSize: 9, color: '#6b7280' }}>Confidence</span>
              <div
                style={{
                  flex: 1,
                  height: 4,
                  background: '#1f2937',
                  borderRadius: 2,
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    width: `${step.confidence * 100}%`,
                    height: '100%',
                    background:
                      step.confidence > 0.7
                        ? '#16a34a'
                        : step.confidence > 0.4
                        ? '#f59e0b'
                        : '#dc2626',
                    borderRadius: 2,
                  }}
                />
              </div>
              <span style={{ fontSize: 9, color: '#8892a4' }}>
                {Math.round(step.confidence * 100)}%
              </span>
            </div>
          )}

          {/* Tool calls */}
          {step.tool_calls?.map((call, i) => (
            <ToolCallDetail
              key={call.tool_call_id ?? i}
              call={call}
            />
          ))}
        </div>
      )}
    </div>
  )
})

// ─── AI Event Card ────────────────────────────────────────────────────────────

const AIEventCard = memo(function AIEventCard({
  event,
}: {
  event: AITraceEvent
}) {
  const [expanded, setExpanded] = useState(false)
  const color = getEventTypeColor(event.event_type)

  // Extract reasoning steps from payload if present
  const steps: ReasoningStep[] = (() => {
    const p = event.payload
    if (Array.isArray(p.steps)) return p.steps as ReasoningStep[]
    if (p.step && typeof p.step === 'object')
      return [p.step as ReasoningStep]
    return []
  })()

  // Extract token info
  const totalTokens =
    (event.payload.total_tokens as number | undefined) ??
    (event.payload.tokens as number | undefined)

  // Extract model info
  const modelName =
    (event.payload.model_name as string | undefined) ??
    (event.payload.model as string | undefined)

  // Extract proposal/suggestion
  const proposal =
    (event.payload.proposal as string | undefined) ??
    (event.payload.suggestion as string | undefined) ??
    (event.payload.patch as string | undefined)

  // Extract requires_human_review
  const requiresReview = Boolean(event.payload.requires_human_review)
  const reviewReason = event.payload.human_review_reason as string | undefined

  return (
    <div
      style={{
        border: `1px solid ${color}30`,
        borderLeft: `3px solid ${color}`,
        borderRadius: 6,
        marginBottom: 8,
        overflow: 'hidden',
        background: `${color}06`,
      }}
    >
      {/* Header */}
      <button
        onClick={() => setExpanded((v) => !v)}
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 8,
          width: '100%',
          padding: '8px 10px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        <div
          style={{
            width: 26,
            height: 26,
            borderRadius: '50%',
            background: `${color}18`,
            border: `1px solid ${color}40`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
            marginTop: 1,
          }}
        >
          <EventTypeIcon type={event.event_type} size={12} />
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
                fontWeight: 700,
                color,
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
              }}
            >
              {event.event_type.replace('AI_', '').replace(/_/g, ' ')}
            </span>
            {requiresReview && (
              <span
                style={{
                  fontSize: 9,
                  fontWeight: 700,
                  color: '#f59e0b',
                  background: 'rgba(245,158,11,0.1)',
                  border: '1px solid rgba(245,158,11,0.3)',
                  borderRadius: 3,
                  padding: '1px 5px',
                }}
              >
                REVIEW NEEDED
              </span>
            )}
          </div>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              flexWrap: 'wrap',
            }}
          >
            <span style={{ fontSize: 9, color: '#4b5563' }}>
              {relTs(event.occurred_at)}
            </span>
            {modelName && (
              <span
                style={{
                  fontSize: 9,
                  color: '#6b7280',
                  background: '#1a1d27',
                  border: '1px solid #2e3347',
                  borderRadius: 3,
                  padding: '1px 4px',
                }}
              >
                {modelName}
              </span>
            )}
            {totalTokens !== undefined && (
              <span style={{ fontSize: 9, color: '#6b7280' }}>
                {totalTokens} tokens
              </span>
            )}
            {steps.length > 0 && (
              <span style={{ fontSize: 9, color: '#6b7280' }}>
                {steps.length} step{steps.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>
        </div>
        {expanded ? (
          <ChevronDown size={12} color="#6b7280" />
        ) : (
          <ChevronRight size={12} color="#6b7280" />
        )}
      </button>

      {/* Expanded content */}
      {expanded && (
        <div style={{ padding: '0 10px 10px' }}>
          {/* Human review banner */}
          {requiresReview && (
            <div
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 8,
                padding: '8px 10px',
                background: 'rgba(245,158,11,0.08)',
                border: '1px solid rgba(245,158,11,0.3)',
                borderRadius: 4,
                marginBottom: 10,
              }}
            >
              <AlertCircle size={13} color="#f59e0b" style={{ flexShrink: 0, marginTop: 1 }} />
              <div>
                <div
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: '#fbbf24',
                    marginBottom: 2,
                  }}
                >
                  Human Review Requested
                </div>
                {reviewReason && (
                  <div style={{ fontSize: 11, color: '#d97706' }}>
                    {reviewReason}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Proposal / suggestion */}
          {proposal && (
            <div style={{ marginBottom: 10 }}>
              <div
                style={{
                  fontSize: 9,
                  fontWeight: 700,
                  color: '#6b7280',
                  textTransform: 'uppercase',
                  letterSpacing: '0.06em',
                  marginBottom: 4,
                }}
              >
                Proposal
              </div>
              <pre
                style={{
                  margin: 0,
                  fontSize: 10,
                  color: '#86efac',
                  fontFamily: 'monospace',
                  background: 'rgba(0,0,0,0.3)',
                  borderRadius: 4,
                  padding: '8px 10px',
                  overflow: 'auto',
                  maxHeight: 200,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                }}
              >
                {proposal}
              </pre>
            </div>
          )}

          {/* Reasoning steps */}
          {steps.length > 0 && (
            <div>
              <div
                style={{
                  fontSize: 9,
                  fontWeight: 700,
                  color: '#6b7280',
                  textTransform: 'uppercase',
                  letterSpacing: '0.06em',
                  marginBottom: 6,
                }}
              >
                Reasoning Steps
              </div>
              {steps.map((step, i) => (
                <ReasoningStepCard
                  key={step.step_id ?? i}
                  step={step}
                />
              ))}
            </div>
          )}

          {/* Raw payload (if no structured data) */}
          {steps.length === 0 && !proposal && (
            <div>
              <div
                style={{
                  fontSize: 9,
                  fontWeight: 700,
                  color: '#6b7280',
                  textTransform: 'uppercase',
                  letterSpacing: '0.06em',
                  marginBottom: 4,
                }}
              >
                Payload
              </div>
              <pre
                style={{
                  margin: 0,
                  fontSize: 10,
                  color: '#cbd5e1',
                  fontFamily: 'monospace',
                  background: 'rgba(0,0,0,0.3)',
                  borderRadius: 4,
                  padding: '8px 10px',
                  overflow: 'auto',
                  maxHeight: 200,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                }}
              >
                {JSON.stringify(event.payload, null, 2)}
              </pre>
            </div>
          )}

          {/* Metadata */}
          <div
            style={{
              marginTop: 8,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              flexWrap: 'wrap',
            }}
          >
            <span
              style={{
                fontSize: 9,
                color: '#4b5563',
                fontFamily: 'monospace',
              }}
              title={event.event_id}
            >
              {event.event_id.slice(0, 12)}…
            </span>
            <span style={{ fontSize: 9, color: '#4b5563' }}>
              {formatTs(event.occurred_at)}
            </span>
            {event.agent_id && (
              <span
                style={{
                  fontSize: 9,
                  color: '#4b5563',
                  fontFamily: 'monospace',
                }}
                title={event.agent_id}
              >
                agent: {event.agent_id.slice(0, 8)}…
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
})

// ─── Token Usage Summary ──────────────────────────────────────────────────────

function TokenUsageSummary({ events }: { events: AITraceEvent[] }) {
  const totalTokens = events.reduce((sum, e) => {
    const t =
      (e.payload.total_tokens as number | undefined) ??
      (e.payload.tokens as number | undefined) ??
      0
    return sum + t
  }, 0)

  const toolCallCount = events.filter(
    (e) => e.event_type === 'AI_TOOL_CALLED'
  ).length

  const reviewCount = events.filter(
    (e) => Boolean(e.payload.requires_human_review)
  ).length

  if (events.length === 0) return null

  return (
    <div
      style={{
        display: 'flex',
        gap: 12,
        padding: '8px 16px',
        background: 'rgba(255,255,255,0.02)',
        borderBottom: '1px solid #2e3347',
        flexShrink: 0,
      }}
    >
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: 14, fontWeight: 700, color: '#e2e8f0' }}>
          {events.length}
        </div>
        <div style={{ fontSize: 9, color: '#6b7280' }}>events</div>
      </div>
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: 14, fontWeight: 700, color: '#a855f7' }}>
          {totalTokens.toLocaleString()}
        </div>
        <div style={{ fontSize: 9, color: '#6b7280' }}>tokens</div>
      </div>
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: 14, fontWeight: 700, color: '#f59e0b' }}>
          {toolCallCount}
        </div>
        <div style={{ fontSize: 9, color: '#6b7280' }}>tool calls</div>
      </div>
      {reviewCount > 0 && (
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: '#f59e0b' }}>
            {reviewCount}
          </div>
          <div style={{ fontSize: 9, color: '#6b7280' }}>reviews</div>
        </div>
      )}
    </div>
  )
}

// ─── Event Type Filter ────────────────────────────────────────────────────────

const EVENT_TYPE_FILTERS = [
  { label: 'All', value: null },
  { label: 'Trace', value: 'AI_REASONING_TRACE' },
  { label: 'Tools', value: 'AI_TOOL_CALLED' },
  { label: 'Review', value: 'AI_REVIEW_COMPLETED' },
  { label: 'Patch', value: 'AI_PATCH_PROPOSED' },
]

// ─── Main AITraceViewer Component ─────────────────────────────────────────────

export function AITraceViewer() {
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)
  const liveEvents = useWorkflowStore((s) => s.events)

  const [historicalEvents, setHistoricalEvents] = useState<AITraceEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [typeFilter, setTypeFilter] = useState<string | null>(null)
  const [autoScroll, setAutoScroll] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)

  // Merge live AI events with historical ones, deduplicated
  const allAIEvents: AITraceEvent[] = (() => {
    const liveAI = liveEvents.filter(isAIEvent).filter(
      (e) =>
        !selectedWorkflowId || e.workflow_id === selectedWorkflowId
    )
    const liveIds = new Set(liveAI.map((e) => e.event_id))
    const merged = [
      ...liveAI,
      ...historicalEvents.filter((e) => !liveIds.has(e.event_id)),
    ]
    // Sort newest first
    return merged.sort(
      (a, b) =>
        new Date(b.occurred_at).getTime() - new Date(a.occurred_at).getTime()
    )
  })()

  const filteredEvents = typeFilter
    ? allAIEvents.filter((e) => e.event_type === typeFilter)
    : allAIEvents

  const loadHistory = useCallback(async () => {
    if (!selectedWorkflowId) return
    setLoading(true)
    setError(null)
    try {
      const res = await apiClient.get<{
        items: FlowEvent[]
        total: number
      }>('/events', {
        params: {
          workflow_id: selectedWorkflowId,
          event_type: 'AI_',
          limit: 200,
          offset: 0,
        },
      })
      const aiEvents = res.data.items.filter(isAIEvent)
      setHistoricalEvents(aiEvents)
    } catch (err) {
      setError(normaliseError(err).message)
    } finally {
      setLoading(false)
    }
  }, [selectedWorkflowId])

  useEffect(() => {
    setHistoricalEvents([])
    setTypeFilter(null)
    loadHistory()
  }, [selectedWorkflowId, loadHistory])

  // Auto-scroll to top (newest) when new events arrive
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = 0
    }
  }, [allAIEvents.length, autoScroll])

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
        <Brain size={32} />
        <p style={{ margin: 0, fontSize: 13 }}>
          Select a workflow to view AI traces
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
              <Brain size={14} color="#a855f7" />
              AI Reasoning Trace
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
                {filteredEvents.length} event
                {filteredEvents.length !== 1 ? 's' : ''} — {selectedWorkflow.name}
              </p>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {/* Auto-scroll toggle */}
            <button
              onClick={() => setAutoScroll((v) => !v)}
              title={autoScroll ? 'Disable auto-scroll' : 'Enable auto-scroll'}
              style={{
                background: autoScroll
                  ? 'rgba(99,102,241,0.15)'
                  : 'transparent',
                border: autoScroll
                  ? '1px solid rgba(99,102,241,0.4)'
                  : '1px solid #2e3347',
                borderRadius: 4,
                cursor: 'pointer',
                color: autoScroll ? '#818cf8' : '#6b7280',
                padding: '3px 6px',
                fontSize: 9,
                fontWeight: 600,
              }}
            >
              LIVE
            </button>
            <button
              onClick={loadHistory}
              disabled={loading}
              title="Refresh AI trace"
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

        {/* Event type filter chips */}
        <div style={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>
          {EVENT_TYPE_FILTERS.map((f) => (
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
                    ? 'rgba(168,85,247,0.2)'
                    : 'transparent',
                color: typeFilter === f.value ? '#c084fc' : '#6b7280',
              }}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {/* Token usage summary */}
      <TokenUsageSummary events={allAIEvents} />

      {/* Content */}
      <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto' }}>
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
              onClick={loadHistory}
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
        ) : filteredEvents.length === 0 && !loading ? (
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
            <Brain size={28} />
            <p style={{ margin: 0, fontSize: 12, textAlign: 'center' }}>
              {typeFilter
                ? `No ${typeFilter.replace('AI_', '')} events yet.`
                : 'No AI reasoning events yet for this workflow.'}
            </p>
            <p style={{ margin: 0, fontSize: 11, color: '#374151' }}>
              AI events will appear here as the workflow runs.
            </p>
          </div>
        ) : (
          <div style={{ padding: '8px 12px' }}>
            {filteredEvents.map((event) => (
              <AIEventCard key={event.event_id} event={event} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
