/**
 * useWorkflowSocket.ts — FlowOS WebSocket Hook
 *
 * Manages a persistent WebSocket connection to the FlowOS API server's
 * /ws endpoint. Incoming events are parsed and dispatched to the Zustand
 * store via pushEvent.
 *
 * Features:
 *   - Auto-reconnect with exponential back-off (max 30 s)
 *   - Heartbeat ping/pong detection
 *   - Optional workflow_id / event_type filter query params
 *   - Cleans up on unmount
 */

import { useEffect, useRef, useCallback } from 'react'
import { useWorkflowStore } from '../store/workflowStore'
import type { FlowEvent } from '../store/workflowStore'

// ─── Constants ────────────────────────────────────────────────────────────────

const WS_BASE_URL =
  import.meta.env.VITE_WS_BASE_URL ??
  (typeof window !== 'undefined'
    ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`
    : 'ws://localhost:8000/ws')

const INITIAL_RECONNECT_DELAY_MS = 1_000
const MAX_RECONNECT_DELAY_MS = 30_000
const RECONNECT_MULTIPLIER = 1.5

// ─── Types ────────────────────────────────────────────────────────────────────

interface UseWorkflowSocketOptions {
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

  const buildUrl = useCallback((): string => {
    const params = new URLSearchParams()
    if (workflowId) params.set('workflow_id', workflowId)
    if (eventType) params.set('event_type', eventType)
    const qs = params.toString()
    return qs ? `${WS_BASE_URL}?${qs}` : WS_BASE_URL
  }, [workflowId, eventType])

  const handleMessage = useCallback(
    (raw: string) => {
      let data: unknown
      try {
        data = JSON.parse(raw)
      } catch {
        return
      }

      if (typeof data !== 'object' || data === null) return

      const msg = data as Record<string, unknown>

      // Heartbeat ping — no action needed
      if (msg.type === 'ping') return

      // Connected acknowledgement
      if (msg.type === 'connected') {
        setWsStatus('connected')
        return
      }

      // It's a FlowOS event envelope
      if (typeof msg.event_type === 'string') {
        const event = msg as unknown as FlowEvent
        pushEvent(event)
      }
    },
    [pushEvent, setWsStatus]
  )

  const connect = useCallback(() => {
    if (!mountedRef.current || disabled) return

    const url = buildUrl()
    setWsStatus('connecting')

    let ws: WebSocket
    try {
      ws = new WebSocket(url)
    } catch (err) {
      console.error('[FlowOS WS] Failed to create WebSocket:', err)
      setWsStatus('error')
      return
    }

    wsRef.current = ws

    ws.onopen = () => {
      if (!mountedRef.current) {
        ws.close()
        return
      }
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
      console.warn('[FlowOS WS] Error:', evt)
      setWsStatus('error')
    }

    ws.onclose = (evt) => {
      wsRef.current = null
      if (!mountedRef.current) return

      setWsStatus('disconnected')
      console.debug(
        `[FlowOS WS] Closed (code=${evt.code}). Reconnecting in ${reconnectDelayRef.current}ms…`
      )

      reconnectTimerRef.current = setTimeout(() => {
        if (mountedRef.current) {
          reconnectDelayRef.current = Math.min(
            reconnectDelayRef.current * RECONNECT_MULTIPLIER,
            MAX_RECONNECT_DELAY_MS
          )
          connect()
        }
      }, reconnectDelayRef.current)
    }
  }, [buildUrl, disabled, handleMessage, setWsStatus])

  useEffect(() => {
    mountedRef.current = true

    if (!disabled) {
      connect()
    }

    return () => {
      mountedRef.current = false

      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }

      if (wsRef.current) {
        wsRef.current.onclose = null // prevent reconnect on intentional close
        wsRef.current.close(1000, 'Component unmounted')
        wsRef.current = null
      }

      setWsStatus('disconnected')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId, eventType, disabled])
}
