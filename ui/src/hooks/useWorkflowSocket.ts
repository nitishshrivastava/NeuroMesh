/**
 * useWorkflowSocket.ts — FlowOS WebSocket Hook
 *
 * Manages a persistent WebSocket connection to the FlowOS API server's
 * /ws endpoint. Incoming events are parsed and dispatched to the Zustand
 * store via pushEvent.
 *
 * Features:
 *   - Absolute WebSocket URL (ws://localhost:8000/ws) for Vite dev compatibility
 *   - Reads VITE_WS_URL from environment (set in ui/.env.local)
 *   - Auto-reconnect with exponential back-off (1s → 30s max)
 *   - Heartbeat ping/pong detection
 *   - Optional workflow_id / event_type filter query params
 *   - Handles both wrapped {"type":"event","data":{...}} and flat event messages
 *   - Cleans up on unmount
 *
 * NOTE on Vite dev proxy:
 *   Vite's HTTP proxy can forward /api/* to the backend, but WebSocket
 *   proxying via the Vite dev server is unreliable. We use an absolute
 *   ws://localhost:8000/ws URL instead of a relative /ws path to connect
 *   directly to the FastAPI backend, bypassing the Vite proxy entirely.
 */

import { useEffect, useRef, useCallback } from 'react'
import { useWorkflowStore } from '../store/workflowStore'
import type { FlowEvent } from '../store/workflowStore'

// ─── Constants ────────────────────────────────────────────────────────────────

/**
 * Base WebSocket URL.
 *
 * Priority:
 *   1. VITE_WS_URL env var (set in ui/.env.local for dev, or build-time for prod)
 *   2. Hard-coded ws://localhost:8000/ws for local development
 *
 * We intentionally do NOT fall back to window.location.host because in Vite
 * dev mode that would be localhost:5173 (the Vite dev server), not the FastAPI
 * backend at localhost:8000.
 */
const WS_BASE_URL: string =
  (import.meta.env.VITE_WS_URL as string | undefined) ?? 'ws://localhost:8000'

/** Full WebSocket endpoint path */
const WS_ENDPOINT = `${WS_BASE_URL}/ws`

const INITIAL_RECONNECT_DELAY_MS = 1_000
const MAX_RECONNECT_DELAY_MS = 30_000
const RECONNECT_MULTIPLIER = 2.0

// ─── Types ────────────────────────────────────────────────────────────────────

export interface UseWorkflowSocketOptions {
  /** Only receive events for this workflow */
  workflowId?: string | null
  /** Only receive events matching this event_type prefix */
  eventType?: string | null
  /** Disable the socket entirely */
  disabled?: boolean
}

// ─── Hook ─────────────────────────────────────────────────────────────────────

export function useWorkflowSocket(
  options: UseWorkflowSocketOptions = {}
): void {
  const { workflowId, eventType, disabled = false } = options

  const pushEvent = useWorkflowStore((s) => s.pushEvent)
  const setWsStatus = useWorkflowStore((s) => s.setWsStatus)

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_DELAY_MS)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  /** Build the full WebSocket URL with optional filter query params */
  const buildUrl = useCallback((): string => {
    const params = new URLSearchParams()
    if (workflowId) params.set('workflow_id', workflowId)
    if (eventType) params.set('event_type', eventType)
    const qs = params.toString()
    return qs ? `${WS_ENDPOINT}?${qs}` : WS_ENDPOINT
  }, [workflowId, eventType])

  /**
   * Parse and dispatch an incoming WebSocket message.
   *
   * The FlowOS backend sends messages in two formats:
   *
   *   1. Wrapped event:  {"type": "event", "data": {<FlowEvent fields>}}
   *   2. Control message: {"type": "connected" | "ping", ...}
   *
   * We handle both formats for forward/backward compatibility.
   */
  const handleMessage = useCallback(
    (raw: string) => {
      let data: unknown
      try {
        data = JSON.parse(raw)
      } catch {
        // Ignore non-JSON messages (e.g. plain text pings)
        return
      }

      if (typeof data !== 'object' || data === null) return

      const msg = data as Record<string, unknown>

      // ── Control messages ──────────────────────────────────────────────────

      // Heartbeat ping — no action needed, browser handles pong at WS level
      if (msg.type === 'ping') return

      // Connection acknowledgement from server
      if (msg.type === 'connected') {
        setWsStatus('connected')
        return
      }

      // ── Event messages ────────────────────────────────────────────────────

      // Format 1: Wrapped event {"type": "event", "data": {<FlowEvent>}}
      if (msg.type === 'event' && typeof msg.data === 'object' && msg.data !== null) {
        const eventData = msg.data as Record<string, unknown>
        if (typeof eventData.event_type === 'string') {
          pushEvent(eventData as unknown as FlowEvent)
        }
        return
      }

      // Format 2: Flat event — event_type at top level (legacy / direct broadcast)
      if (typeof msg.event_type === 'string') {
        pushEvent(msg as unknown as FlowEvent)
        return
      }
    },
    [pushEvent, setWsStatus]
  )

  /** Open a new WebSocket connection with full lifecycle management */
  const connect = useCallback(() => {
    if (!mountedRef.current || disabled) return

    // Don't open a second connection if one is already open/connecting
    if (
      wsRef.current &&
      (wsRef.current.readyState === WebSocket.OPEN ||
        wsRef.current.readyState === WebSocket.CONNECTING)
    ) {
      return
    }

    const url = buildUrl()
    setWsStatus('connecting')
    console.debug('[FlowOS WS] Connecting to', url)

    let ws: WebSocket
    try {
      ws = new WebSocket(url)
    } catch (err) {
      console.error('[FlowOS WS] Failed to create WebSocket:', err)
      setWsStatus('error')
      // Schedule reconnect even on construction failure
      scheduleReconnect()
      return
    }

    wsRef.current = ws

    ws.onopen = () => {
      if (!mountedRef.current) {
        ws.close()
        return
      }
      // Reset backoff on successful connection
      reconnectDelayRef.current = INITIAL_RECONNECT_DELAY_MS
      setWsStatus('connected')
      console.debug('[FlowOS WS] Connected to', url)
    }

    ws.onmessage = (evt) => {
      if (mountedRef.current) {
        handleMessage(evt.data as string)
      }
    }

    ws.onerror = (evt) => {
      console.warn('[FlowOS WS] Socket error:', evt)
      setWsStatus('error')
      // onclose will fire after onerror, which handles reconnect
    }

    ws.onclose = (evt) => {
      wsRef.current = null
      if (!mountedRef.current) return

      setWsStatus('disconnected')
      console.debug(
        `[FlowOS WS] Closed (code=${evt.code}, reason="${evt.reason}"). ` +
          `Reconnecting in ${reconnectDelayRef.current}ms…`
      )
      scheduleReconnect()
    }
  }, [buildUrl, disabled, handleMessage, setWsStatus]) // eslint-disable-line react-hooks/exhaustive-deps

  /** Schedule a reconnect attempt with exponential backoff */
  const scheduleReconnect = useCallback(() => {
    if (!mountedRef.current || disabled) return

    // Clear any existing reconnect timer
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current)
    }

    const delay = reconnectDelayRef.current

    reconnectTimerRef.current = setTimeout(() => {
      if (mountedRef.current && !disabled) {
        // Increase delay for next attempt (exponential backoff, capped at max)
        reconnectDelayRef.current = Math.min(
          reconnectDelayRef.current * RECONNECT_MULTIPLIER,
          MAX_RECONNECT_DELAY_MS
        )
        connect()
      }
    }, delay)
  }, [connect, disabled])

  useEffect(() => {
    mountedRef.current = true

    if (!disabled) {
      connect()
    }

    return () => {
      mountedRef.current = false

      // Cancel any pending reconnect timer
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }

      // Close the WebSocket cleanly (suppress reconnect by nulling onclose first)
      if (wsRef.current) {
        wsRef.current.onclose = null // prevent reconnect on intentional close
        wsRef.current.onerror = null
        wsRef.current.onmessage = null
        wsRef.current.close(1000, 'Component unmounted')
        wsRef.current = null
      }

      setWsStatus('disconnected')
    }
    // Re-run when workflowId/eventType/disabled change — creates a fresh connection
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId, eventType, disabled])
}
