/**
 * EventTimeline.tsx — FlowOS Live Event Feed
 *
 * Displays a real-time scrolling timeline of Kafka events received via
 * WebSocket. Events are colour-coded by domain (workflow, task, agent,
 * build, AI, workspace, policy, observability).
 *
 * Features:
 *   - Auto-scroll to newest event (toggleable)
 *   - Event type filter (prefix-based)
 *   - Workflow-scoped view when a workflow is selected
 *   - Expandable event payload viewer
 *   - Event count badge
 *   - Clear button
 */

import React, { useRef, useEffect, useState, useCallback, memo } from 'react'
import { formatDistanceToNow } from 'date-fns'
import {
  Activity,
  ChevronDown,
  ChevronRight,
  Trash2,
  Filter,
  Wifi,
  WifiOff,
  Loader2,
  AlertCircle,
} from 'lucide-react'
import {
  useWorkflowStore,
  selectWorkflowEvents,
  type FlowEvent,
  type WsStatus,
} from '../../store/workflowStore'

// ─── Event domain colour mapping ──────────────────────────────────────────────

const DOMAIN_COLORS: Record<string, string> = {
  WORKFLOW: '#6366f1',  // indigo
  TASK: '#f59e0b',      // amber
  AGENT: '#22c55e',     // green
  BUILD: '#3b82f6',     // blue
  TEST: '#3b82f6',      // blue
  ARTIFACT: '#3b82f6',  // blue
  AI: '#a855f7',        // purple
  WORKSPACE: '#14b8a6', // teal
  CHECKPOINT: '#14b8a6',
  BRANCH: '#14b8a6',
  HANDOFF: '#14b8a6',
  SNAPSHOT: '#14b8a6',
  POLICY: '#f97316',    // orange
  APPROVAL: '#f97316',
  METRIC: '#6b7280',    // gray
  TRACE: '#6b7280',
  HEALTH: '#6b7280',
  SYSTEM: '#ef4444',    // red
}

function getDomainColor(eventType: string): string {
  const prefix = eventType.split('_')[0]
  return DOMAIN_COLORS[prefix] ?? '#6b7280'
}

function getDomainLabel(eventType: string): string {
  return eventType.split('_')[0]
}

// ─── WebSocket status indicator ───────────────────────────────────────────────

function WsStatusIndicator({ status }: { status: WsStatus }) {
  const configs: Record<WsStatus, { icon: React.ReactNode; color: string; label: string }> = {
    connected: {
      icon: <Wifi size={11} />,
      color: '#22c55e',
      label: 'Live',
    },
    connecting: {
      icon: <Loader2 size={11} className="animate-spin" />,
      color: '#f59e0b',
      label: 'Connecting',
    },
    disconnected: {
      icon: <WifiOff size={11} />,
      color: '#6b7280',
      label: 'Offline',
    },
    error: {
      icon: <AlertCircle size={11} />,
      color: '#ef4444',
      label: 'Error',
    },
  }

  const cfg = configs[status]
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 4,
        color: cfg.color,
        fontSize: 10,
        fontWeight: 600,
      }}
    >
      {cfg.icon}
      {cfg.label}
    </div>
  )
}

// ─── Event Filter Bar ─────────────────────────────────────────────────────────

const FILTER_OPTIONS = [
  { label: 'All', value: null },
  { label: 'Workflow', value: 'WORKFLOW' },
  { label: 'Task', value: 'TASK' },
  { label: 'Agent', value: 'AGENT' },
  { label: 'Build', value: 'BUILD' },
  { label: 'AI', value: 'AI' },
  { label: 'Policy', value: 'POLICY' },
]

function EventFilterBar() {
  const eventFilter = useWorkflowStore((s) => s.eventFilter)
  const setEventFilter = useWorkflowStore((s) => s.setEventFilter)

  return (
    <div
      style={{
        display: 'flex',
        gap: 4,
        overflowX: 'auto',
        padding: '0 4px',
      }}
    >
      {FILTER_OPTIONS.map((opt) => (
        <button
          key={String(opt.value)}
          onClick={() => setEventFilter(opt.value)}
          style={{
            fontSize: 10,
            fontWeight: 600,
            padding: '3px 8px',
            borderRadius: 4,
            border: 'none',
            cursor: 'pointer',
            background:
              eventFilter === opt.value
                ? 'rgba(99,102,241,0.2)'
                : 'transparent',
            color: eventFilter === opt.value ? '#818cf8' : '#8892a4',
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

// ─── Single Event Row ─────────────────────────────────────────────────────────

const EventRow = memo(function EventRow({ event }: { event: FlowEvent }) {
  const [expanded, setExpanded] = useState(false)
  const color = getDomainColor(event.event_type)
  const domain = getDomainLabel(event.event_type)

  const relTime = (() => {
    try {
      return formatDistanceToNow(new Date(event.occurred_at), {
        addSuffix: true,
        includeSeconds: true,
      })
    } catch {
      return event.occurred_at
    }
  })()

  const hasPayload =
    event.payload && Object.keys(event.payload).length > 0

  return (
    <div
      style={{
        borderBottom: '1px solid #1a1d27',
        transition: 'background 0.1s ease',
      }}
    >
      {/* Main row */}
      <div
        onClick={() => hasPayload && setExpanded((v) => !v)}
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 8,
          padding: '7px 12px',
          cursor: hasPayload ? 'pointer' : 'default',
        }}
        onMouseEnter={(e) => {
          ;(e.currentTarget as HTMLDivElement).style.background =
            'rgba(255,255,255,0.03)'
        }}
        onMouseLeave={(e) => {
          ;(e.currentTarget as HTMLDivElement).style.background = 'transparent'
        }}
      >
        {/* Domain colour bar */}
        <div
          style={{
            width: 3,
            height: 36,
            background: color,
            borderRadius: 2,
            flexShrink: 0,
            marginTop: 1,
          }}
        />

        {/* Content */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              marginBottom: 3,
            }}
          >
            {/* Domain badge */}
            <span
              style={{
                fontSize: 9,
                fontWeight: 700,
                color,
                background: `${color}20`,
                border: `1px solid ${color}40`,
                borderRadius: 3,
                padding: '1px 4px',
                textTransform: 'uppercase',
                letterSpacing: '0.05em',
                flexShrink: 0,
              }}
            >
              {domain}
            </span>

            {/* Event type */}
            <span
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: '#e2e8f0',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {event.event_type}
            </span>

            {/* Expand icon */}
            {hasPayload && (
              <span style={{ marginLeft: 'auto', color: '#4b5563', flexShrink: 0 }}>
                {expanded ? (
                  <ChevronDown size={11} />
                ) : (
                  <ChevronRight size={11} />
                )}
              </span>
            )}
          </div>

          {/* Meta row */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              flexWrap: 'wrap',
            }}
          >
            <span style={{ fontSize: 10, color: '#4b5563' }}>{relTime}</span>
            {event.workflow_id && (
              <span
                style={{
                  fontSize: 9,
                  color: '#6b7280',
                  fontFamily: 'monospace',
                }}
              >
                wf:{event.workflow_id.slice(0, 8)}
              </span>
            )}
            {event.task_id && (
              <span
                style={{
                  fontSize: 9,
                  color: '#6b7280',
                  fontFamily: 'monospace',
                }}
              >
                task:{event.task_id.slice(0, 8)}
              </span>
            )}
            {event.agent_id && (
              <span
                style={{
                  fontSize: 9,
                  color: '#6b7280',
                  fontFamily: 'monospace',
                }}
              >
                agent:{event.agent_id.slice(0, 10)}
              </span>
            )}
            {event.severity && event.severity !== 'info' && (
              <span
                style={{
                  fontSize: 9,
                  fontWeight: 700,
                  color:
                    event.severity === 'error'
                      ? '#ef4444'
                      : event.severity === 'warning'
                      ? '#f59e0b'
                      : '#6b7280',
                  textTransform: 'uppercase',
                }}
              >
                {event.severity}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Expanded payload */}
      {expanded && hasPayload && (
        <div
          style={{
            padding: '0 12px 10px 23px',
          }}
        >
          <pre
            style={{
              margin: 0,
              fontSize: 10,
              color: '#8892a4',
              background: '#0f1117',
              border: '1px solid #2e3347',
              borderRadius: 6,
              padding: '8px 10px',
              overflow: 'auto',
              maxHeight: 200,
              lineHeight: 1.5,
            }}
          >
            {JSON.stringify(event.payload, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
})

// ─── Main EventTimeline ───────────────────────────────────────────────────────

export function EventTimeline() {
  const wsStatus = useWorkflowStore((s) => s.wsStatus)
  const eventFilter = useWorkflowStore((s) => s.eventFilter)
  const clearEvents = useWorkflowStore((s) => s.clearEvents)
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)

  // Use workflow-scoped events when a workflow is selected
  const allEvents = useWorkflowStore(
    selectedWorkflowId ? selectWorkflowEvents : (s) => s.events
  )

  // Apply event type filter
  const events = eventFilter
    ? allEvents.filter((e) => e.event_type.startsWith(eventFilter))
    : allEvents

  const [autoScroll, setAutoScroll] = useState(true)
  const listRef = useRef<HTMLDivElement>(null)
  const prevCountRef = useRef(0)

  // Auto-scroll to top (newest events are at top)
  useEffect(() => {
    if (autoScroll && events.length !== prevCountRef.current) {
      prevCountRef.current = events.length
      if (listRef.current) {
        listRef.current.scrollTop = 0
      }
    }
  }, [events.length, autoScroll])

  const handleScroll = useCallback(() => {
    if (!listRef.current) return
    // If user scrolled away from top, disable auto-scroll
    const atTop = listRef.current.scrollTop < 50
    setAutoScroll(atTop)
  }, [])

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        background: '#1a1d27',
        borderTop: '1px solid #2e3347',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: '10px 12px',
          borderBottom: '1px solid #2e3347',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flexShrink: 0,
        }}
      >
        <Activity size={13} color="#6366f1" />
        <span
          style={{ fontSize: 12, fontWeight: 600, color: '#e2e8f0', flex: 1 }}
        >
          Event Feed
        </span>

        {/* Event count */}
        <span
          style={{
            fontSize: 10,
            color: '#8892a4',
            background: '#0f1117',
            border: '1px solid #2e3347',
            borderRadius: 10,
            padding: '1px 6px',
          }}
        >
          {events.length}
        </span>

        {/* Auto-scroll toggle */}
        <button
          onClick={() => setAutoScroll((v) => !v)}
          title={autoScroll ? 'Auto-scroll ON' : 'Auto-scroll OFF'}
          style={{
            background: autoScroll ? 'rgba(99,102,241,0.15)' : 'transparent',
            border: `1px solid ${autoScroll ? 'rgba(99,102,241,0.4)' : '#2e3347'}`,
            borderRadius: 4,
            cursor: 'pointer',
            color: autoScroll ? '#818cf8' : '#6b7280',
            padding: '2px 6px',
            fontSize: 9,
            fontWeight: 700,
          }}
        >
          AUTO
        </button>

        {/* Clear */}
        <button
          onClick={clearEvents}
          title="Clear events"
          style={{
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            color: '#6b7280',
            padding: 2,
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <Trash2 size={12} />
        </button>

        {/* WS status */}
        <WsStatusIndicator status={wsStatus} />
      </div>

      {/* Filter bar */}
      <div
        style={{
          padding: '6px 8px',
          borderBottom: '1px solid #2e3347',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          flexShrink: 0,
        }}
      >
        <Filter size={10} color="#6b7280" />
        <EventFilterBar />
      </div>

      {/* Event list */}
      <div
        ref={listRef}
        onScroll={handleScroll}
        style={{
          flex: 1,
          overflowY: 'auto',
        }}
      >
        {events.length === 0 ? (
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
            <Activity size={24} color="#2e3347" />
            <p
              style={{
                color: '#4b5563',
                fontSize: 12,
                textAlign: 'center',
                margin: 0,
              }}
            >
              {wsStatus === 'connected'
                ? 'Waiting for events…'
                : wsStatus === 'connecting'
                ? 'Connecting to event stream…'
                : 'Not connected to event stream'}
            </p>
          </div>
        ) : (
          events.map((event) => (
            <EventRow key={event.event_id} event={event} />
          ))
        )}
      </div>
    </div>
  )
}
