/**
 * ReplayControls.tsx — FlowOS Event Replay Controls
 *
 * Provides a time-scrubbing interface to replay the event history of a
 * selected workflow. Users can:
 *   - Scrub through the event timeline using a slider
 *   - Play/pause the replay at configurable speeds (0.5x, 1x, 2x, 5x)
 *   - Jump to specific events by clicking on the timeline
 *   - See the "current" event highlighted in the EventTimeline
 *   - Export the event log as JSON
 *
 * The replay state is local — it does not affect the live WebSocket feed.
 * Events are loaded from GET /events?workflow_id=... on mount.
 */

import React, {
  useState,
  useEffect,
  useCallback,
  useRef,
} from 'react'
import { format } from 'date-fns'
import {
  Play,
  Pause,
  SkipBack,
  SkipForward,
  FastForward,
  RefreshCw,
  Download,
  AlertCircle,
  Loader2,
  ChevronLeft,
  ChevronRight,
  Activity,
} from 'lucide-react'
import { useWorkflowStore, type FlowEvent } from '../../store/workflowStore'
import { normaliseError, listWorkflowEvents } from '../../api/client'

// ─── Types ────────────────────────────────────────────────────────────────────

type PlaybackSpeed = 0.5 | 1 | 2 | 5 | 10

// ─── Helpers ──────────────────────────────────────────────────────────────────

const DOMAIN_COLORS: Record<string, string> = {
  WORKFLOW: '#6366f1',
  TASK: '#f59e0b',
  AGENT: '#22c55e',
  BUILD: '#3b82f6',
  TEST: '#3b82f6',
  AI: '#a855f7',
  WORKSPACE: '#14b8a6',
  CHECKPOINT: '#14b8a6',
  HANDOFF: '#14b8a6',
  POLICY: '#f97316',
  METRIC: '#6b7280',
  SYSTEM: '#ef4444',
}

function getDomainColor(eventType: string): string {
  const domain = eventType.split('_')[0]
  return DOMAIN_COLORS[domain] ?? '#6b7280'
}

function formatTs(ts: string): string {
  try {
    return format(new Date(ts), 'HH:mm:ss.SSS')
  } catch {
    return ts
  }
}

function formatDate(ts: string): string {
  try {
    return format(new Date(ts), 'MMM d, HH:mm:ss')
  } catch {
    return ts
  }
}

// ─── Timeline Minimap ─────────────────────────────────────────────────────────

function TimelineMinimap({
  events,
  currentIndex,
  onSeek,
}: {
  events: FlowEvent[]
  currentIndex: number
  onSeek: (index: number) => void
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  // Draw the minimap
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || events.length === 0) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const W = canvas.width
    const H = canvas.height
    ctx.clearRect(0, 0, W, H)

    // Background
    ctx.fillStyle = '#0f1117'
    ctx.fillRect(0, 0, W, H)

    // Draw event ticks
    events.forEach((event, i) => {
      const x = (i / (events.length - 1 || 1)) * W
      const color = getDomainColor(event.event_type)
      ctx.fillStyle = color + '80'
      ctx.fillRect(x - 1, 0, 2, H)
    })

    // Draw current position indicator
    if (events.length > 0) {
      const x = (currentIndex / (events.length - 1 || 1)) * W
      ctx.fillStyle = '#6366f1'
      ctx.fillRect(x - 1, 0, 2, H)
      // Glow
      ctx.shadowColor = '#6366f1'
      ctx.shadowBlur = 6
      ctx.fillRect(x - 1, 0, 2, H)
      ctx.shadowBlur = 0
    }
  }, [events, currentIndex])

  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current
      if (!canvas || events.length === 0) return
      const rect = canvas.getBoundingClientRect()
      const x = e.clientX - rect.left
      const ratio = x / rect.width
      const index = Math.round(ratio * (events.length - 1))
      onSeek(Math.max(0, Math.min(events.length - 1, index)))
    },
    [events.length, onSeek]
  )

  return (
    <div
      ref={containerRef}
      style={{
        position: 'relative',
        height: 32,
        background: '#0f1117',
        border: '1px solid #2e3347',
        borderRadius: 4,
        overflow: 'hidden',
        cursor: 'crosshair',
      }}
    >
      <canvas
        ref={canvasRef}
        width={800}
        height={32}
        onClick={handleClick}
        style={{ width: '100%', height: '100%', display: 'block' }}
      />
    </div>
  )
}

// ─── Event Row ────────────────────────────────────────────────────────────────

function EventRow({
  event,
  isCurrent,
  onClick,
}: {
  event: FlowEvent
  isCurrent: boolean
  onClick: () => void
}) {
  const color = getDomainColor(event.event_type)

  return (
    <button
      onClick={onClick}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        width: '100%',
        padding: '5px 10px',
        background: isCurrent
          ? 'rgba(99,102,241,0.15)'
          : 'transparent',
        border: isCurrent
          ? '1px solid rgba(99,102,241,0.4)'
          : '1px solid transparent',
        borderRadius: 4,
        cursor: 'pointer',
        textAlign: 'left',
        transition: 'background 0.1s ease',
        marginBottom: 1,
      }}
      onMouseEnter={(e) => {
        if (!isCurrent)
          (e.currentTarget as HTMLButtonElement).style.background =
            'rgba(255,255,255,0.04)'
      }}
      onMouseLeave={(e) => {
        if (!isCurrent)
          (e.currentTarget as HTMLButtonElement).style.background = 'transparent'
      }}
    >
      <div
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: color,
          flexShrink: 0,
          boxShadow: isCurrent ? `0 0 6px ${color}` : 'none',
        }}
      />
      <span
        style={{
          fontSize: 10,
          fontWeight: isCurrent ? 700 : 500,
          color: isCurrent ? '#e2e8f0' : '#8892a4',
          fontFamily: 'monospace',
          flex: 1,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {event.event_type}
      </span>
      <span
        style={{
          fontSize: 9,
          color: '#4b5563',
          fontFamily: 'monospace',
          flexShrink: 0,
        }}
      >
        {formatTs(event.occurred_at)}
      </span>
    </button>
  )
}

// ─── Main ReplayControls Component ───────────────────────────────────────────

export function ReplayControls() {
  const selectedWorkflowId = useWorkflowStore((s) => s.selectedWorkflowId)
  const selectedWorkflow = useWorkflowStore((s) => s.selectedWorkflow)

  const [events, setEvents] = useState<FlowEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [currentIndex, setCurrentIndex] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [speed, setSpeed] = useState<PlaybackSpeed>(1)

  const playIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const eventListRef = useRef<HTMLDivElement>(null)

  const currentEvent = events[currentIndex] ?? null

  // ── Load events ───────────────────────────────────────────────────────────

  const loadEvents = useCallback(async () => {
    if (!selectedWorkflowId) return
    setLoading(true)
    setError(null)
    try {
      const res = await listWorkflowEvents(selectedWorkflowId)
      // Sort oldest first for replay
      const sorted = [...res.items].sort(
        (a, b) =>
          new Date(a.occurred_at).getTime() - new Date(b.occurred_at).getTime()
      )
      setEvents(sorted)
      setCurrentIndex(0)
      setIsPlaying(false)
    } catch (err) {
      setError(normaliseError(err).message)
    } finally {
      setLoading(false)
    }
  }, [selectedWorkflowId])

  useEffect(() => {
    setEvents([])
    setCurrentIndex(0)
    setIsPlaying(false)
    loadEvents()
  }, [selectedWorkflowId, loadEvents])

  // ── Playback engine ───────────────────────────────────────────────────────

  useEffect(() => {
    if (playIntervalRef.current !== null) {
      clearInterval(playIntervalRef.current)
      playIntervalRef.current = null
    }

    if (!isPlaying || events.length === 0) return

    const intervalMs = 1000 / speed

    playIntervalRef.current = setInterval(() => {
      setCurrentIndex((prev) => {
        if (prev >= events.length - 1) {
          setIsPlaying(false)
          return prev
        }
        return prev + 1
      })
    }, intervalMs)

    return () => {
      if (playIntervalRef.current !== null) {
        clearInterval(playIntervalRef.current)
        playIntervalRef.current = null
      }
    }
  }, [isPlaying, speed, events.length])

  // ── Auto-scroll event list to current item ────────────────────────────────

  useEffect(() => {
    if (!eventListRef.current) return
    const container = eventListRef.current
    const item = container.children[currentIndex] as HTMLElement | undefined
    if (item) {
      item.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  }, [currentIndex])

  // ── Controls ──────────────────────────────────────────────────────────────

  const handlePlayPause = useCallback(() => {
    if (currentIndex >= events.length - 1 && !isPlaying) {
      // Restart from beginning
      setCurrentIndex(0)
      setIsPlaying(true)
    } else {
      setIsPlaying((v) => !v)
    }
  }, [currentIndex, events.length, isPlaying])

  const handleSkipBack = useCallback(() => {
    setIsPlaying(false)
    setCurrentIndex(0)
  }, [])

  const handleSkipForward = useCallback(() => {
    setIsPlaying(false)
    setCurrentIndex(events.length - 1)
  }, [events.length])

  const handleStepBack = useCallback(() => {
    setIsPlaying(false)
    setCurrentIndex((prev) => Math.max(0, prev - 1))
  }, [])

  const handleStepForward = useCallback(() => {
    setIsPlaying(false)
    setCurrentIndex((prev) => Math.min(events.length - 1, prev + 1))
  }, [events.length])

  const handleSeek = useCallback((index: number) => {
    setIsPlaying(false)
    setCurrentIndex(index)
  }, [])

  const handleSliderChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setIsPlaying(false)
      setCurrentIndex(Number(e.target.value))
    },
    []
  )

  // ── Export ────────────────────────────────────────────────────────────────

  const handleExport = useCallback(() => {
    if (events.length === 0) return
    const blob = new Blob([JSON.stringify(events, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `flowos-events-${selectedWorkflowId?.slice(0, 8) ?? 'unknown'}.json`
    a.click()
    URL.revokeObjectURL(url)
  }, [events, selectedWorkflowId])

  // ── Progress ──────────────────────────────────────────────────────────────

  const progressPct =
    events.length > 1 ? (currentIndex / (events.length - 1)) * 100 : 0

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
        <Activity size={32} />
        <p style={{ margin: 0, fontSize: 13 }}>
          Select a workflow to replay events
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
            marginBottom: 4,
          }}
        >
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
            <Activity size={14} color="#6366f1" />
            Event Replay
          </h2>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <button
              onClick={handleExport}
              disabled={events.length === 0}
              title="Export events as JSON"
              style={{
                background: 'transparent',
                border: '1px solid #2e3347',
                borderRadius: 4,
                cursor: events.length === 0 ? 'not-allowed' : 'pointer',
                color: events.length === 0 ? '#374151' : '#6b7280',
                padding: '3px 6px',
                display: 'flex',
                alignItems: 'center',
                gap: 4,
                fontSize: 10,
              }}
            >
              <Download size={11} />
              Export
            </button>
            <button
              onClick={loadEvents}
              disabled={loading}
              title="Reload events"
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
        {selectedWorkflow && (
          <p style={{ margin: 0, fontSize: 11, color: '#8892a4' }}>
            {events.length} events — {selectedWorkflow.name}
          </p>
        )}
      </div>

      {/* Error state */}
      {error && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '8px 16px',
            background: 'rgba(239,68,68,0.1)',
            borderBottom: '1px solid rgba(239,68,68,0.3)',
            fontSize: 11,
            color: '#f87171',
            flexShrink: 0,
          }}
        >
          <AlertCircle size={12} />
          {error}
        </div>
      )}

      {/* Playback controls */}
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid #2e3347',
          flexShrink: 0,
        }}
      >
        {/* Timeline minimap */}
        <div style={{ marginBottom: 10 }}>
          <TimelineMinimap
            events={events}
            currentIndex={currentIndex}
            onSeek={handleSeek}
          />
        </div>

        {/* Slider */}
        <div style={{ marginBottom: 10 }}>
          <input
            type="range"
            min={0}
            max={Math.max(0, events.length - 1)}
            value={currentIndex}
            onChange={handleSliderChange}
            disabled={events.length === 0}
            style={{
              width: '100%',
              accentColor: '#6366f1',
              cursor: events.length === 0 ? 'not-allowed' : 'pointer',
            }}
          />
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              marginTop: 2,
            }}
          >
            <span style={{ fontSize: 9, color: '#4b5563' }}>
              {events.length > 0 ? formatTs(events[0].occurred_at) : '—'}
            </span>
            <span style={{ fontSize: 9, color: '#6366f1', fontWeight: 600 }}>
              {progressPct.toFixed(0)}%
            </span>
            <span style={{ fontSize: 9, color: '#4b5563' }}>
              {events.length > 0
                ? formatTs(events[events.length - 1].occurred_at)
                : '—'}
            </span>
          </div>
        </div>

        {/* Transport controls */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 6,
            marginBottom: 10,
          }}
        >
          <button
            onClick={handleSkipBack}
            disabled={events.length === 0 || currentIndex === 0}
            title="Jump to start"
            style={controlBtnStyle(
              events.length === 0 || currentIndex === 0
            )}
          >
            <SkipBack size={14} />
          </button>
          <button
            onClick={handleStepBack}
            disabled={events.length === 0 || currentIndex === 0}
            title="Step back"
            style={controlBtnStyle(
              events.length === 0 || currentIndex === 0
            )}
          >
            <ChevronLeft size={14} />
          </button>
          <button
            onClick={handlePlayPause}
            disabled={events.length === 0}
            title={isPlaying ? 'Pause' : 'Play'}
            style={{
              ...controlBtnStyle(events.length === 0),
              width: 36,
              height: 36,
              background: events.length === 0
                ? 'rgba(99,102,241,0.05)'
                : 'rgba(99,102,241,0.15)',
              border: '1px solid rgba(99,102,241,0.4)',
              color: '#818cf8',
            }}
          >
            {isPlaying ? <Pause size={16} /> : <Play size={16} />}
          </button>
          <button
            onClick={handleStepForward}
            disabled={events.length === 0 || currentIndex >= events.length - 1}
            title="Step forward"
            style={controlBtnStyle(
              events.length === 0 || currentIndex >= events.length - 1
            )}
          >
            <ChevronRight size={14} />
          </button>
          <button
            onClick={handleSkipForward}
            disabled={
              events.length === 0 || currentIndex >= events.length - 1
            }
            title="Jump to end"
            style={controlBtnStyle(
              events.length === 0 || currentIndex >= events.length - 1
            )}
          >
            <SkipForward size={14} />
          </button>
        </div>

        {/* Speed selector + position info */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <FastForward size={11} color="#6b7280" />
            <span style={{ fontSize: 10, color: '#6b7280' }}>Speed:</span>
            {([0.5, 1, 2, 5, 10] as PlaybackSpeed[]).map((s) => (
              <button
                key={s}
                onClick={() => setSpeed(s)}
                style={{
                  fontSize: 9,
                  fontWeight: 600,
                  padding: '2px 5px',
                  borderRadius: 3,
                  border: 'none',
                  cursor: 'pointer',
                  background:
                    speed === s ? 'rgba(99,102,241,0.2)' : 'transparent',
                  color: speed === s ? '#818cf8' : '#6b7280',
                }}
              >
                {s}x
              </button>
            ))}
          </div>
          <span style={{ fontSize: 10, color: '#6b7280' }}>
            {events.length > 0
              ? `${currentIndex + 1} / ${events.length}`
              : '0 / 0'}
          </span>
        </div>
      </div>

      {/* Current event detail */}
      {currentEvent && (
        <div
          style={{
            padding: '10px 16px',
            borderBottom: '1px solid #2e3347',
            flexShrink: 0,
            background: 'rgba(99,102,241,0.05)',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              marginBottom: 6,
            }}
          >
            <div
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: getDomainColor(currentEvent.event_type),
                boxShadow: `0 0 6px ${getDomainColor(currentEvent.event_type)}`,
                flexShrink: 0,
              }}
            />
            <span
              style={{
                fontSize: 12,
                fontWeight: 700,
                color: getDomainColor(currentEvent.event_type),
                fontFamily: 'monospace',
              }}
            >
              {currentEvent.event_type}
            </span>
            <span
              style={{
                fontSize: 10,
                color: '#6b7280',
                marginLeft: 'auto',
                fontFamily: 'monospace',
              }}
            >
              {formatDate(currentEvent.occurred_at)}
            </span>
          </div>
          <div
            style={{
              display: 'flex',
              gap: 12,
              flexWrap: 'wrap',
            }}
          >
            {currentEvent.source && (
              <span style={{ fontSize: 10, color: '#6b7280' }}>
                source: <span style={{ color: '#8892a4' }}>{currentEvent.source}</span>
              </span>
            )}
            {currentEvent.agent_id && (
              <span style={{ fontSize: 10, color: '#6b7280' }}>
                agent:{' '}
                <span
                  style={{ color: '#8892a4', fontFamily: 'monospace' }}
                  title={currentEvent.agent_id}
                >
                  {currentEvent.agent_id.slice(0, 8)}…
                </span>
              </span>
            )}
            {currentEvent.task_id && (
              <span style={{ fontSize: 10, color: '#6b7280' }}>
                task:{' '}
                <span
                  style={{ color: '#8892a4', fontFamily: 'monospace' }}
                  title={currentEvent.task_id}
                >
                  {currentEvent.task_id.slice(0, 8)}…
                </span>
              </span>
            )}
          </div>
          {Object.keys(currentEvent.payload).length > 0 && (
            <details style={{ marginTop: 6 }}>
              <summary
                style={{
                  fontSize: 10,
                  color: '#6b7280',
                  cursor: 'pointer',
                  userSelect: 'none',
                }}
              >
                Payload
              </summary>
              <pre
                style={{
                  margin: '4px 0 0',
                  fontSize: 10,
                  color: '#cbd5e1',
                  fontFamily: 'monospace',
                  background: 'rgba(0,0,0,0.3)',
                  borderRadius: 3,
                  padding: '6px 8px',
                  overflow: 'auto',
                  maxHeight: 100,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                }}
              >
                {JSON.stringify(currentEvent.payload, null, 2)}
              </pre>
            </details>
          )}
        </div>
      )}

      {/* Event list */}
      <div
        ref={eventListRef}
        style={{ flex: 1, overflowY: 'auto', padding: '6px 8px' }}
      >
        {loading ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 8,
              padding: 24,
              color: '#6b7280',
              fontSize: 12,
            }}
          >
            <Loader2 size={16} style={{ animation: 'spin 1s linear infinite' }} />
            Loading events…
          </div>
        ) : events.length === 0 ? (
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
            <Activity size={28} />
            <p style={{ margin: 0, fontSize: 12, textAlign: 'center' }}>
              No events recorded for this workflow.
            </p>
          </div>
        ) : (
          events.map((event, i) => (
            <EventRow
              key={event.event_id}
              event={event}
              isCurrent={i === currentIndex}
              onClick={() => handleSeek(i)}
            />
          ))
        )}
      </div>
    </div>
  )
}

// ─── Style helpers ────────────────────────────────────────────────────────────

function controlBtnStyle(disabled: boolean): React.CSSProperties {
  return {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 30,
    height: 30,
    background: disabled ? 'transparent' : 'rgba(255,255,255,0.04)',
    border: `1px solid ${disabled ? '#1f2937' : '#2e3347'}`,
    borderRadius: 6,
    cursor: disabled ? 'not-allowed' : 'pointer',
    color: disabled ? '#374151' : '#8892a4',
    transition: 'background 0.1s ease',
  }
}
