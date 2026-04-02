/**
 * api/client.ts — FlowOS REST API Client
 *
 * Thin wrapper around axios that:
 *   - Points at the FlowOS FastAPI backend (proxied via Vite in dev)
 *   - Provides typed request/response helpers for all major endpoints
 *   - Handles error normalisation
 *
 * Base URL: /api  (Vite dev proxy → http://localhost:8000)
 * In production, set VITE_API_BASE_URL env var.
 */

import axios, { AxiosError } from 'axios'
import type { AxiosInstance, AxiosResponse } from 'axios'
import type { Workflow, Task, FlowEvent, Agent } from '../store/workflowStore'

// Re-export Agent so existing imports of `type Agent` from this module keep working
export type { Agent }

// ─── Axios instance ───────────────────────────────────────────────────────────

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api'

export const apiClient: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 15_000,
  headers: {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  },
})

// ─── Error normalisation ──────────────────────────────────────────────────────

export interface ApiError {
  message: string
  status: number | null
  detail: unknown
}

export function normaliseError(err: unknown): ApiError {
  if (err instanceof AxiosError) {
    const status = err.response?.status ?? null
    const detail = err.response?.data ?? err.message
    const message =
      typeof detail === 'object' && detail !== null && 'detail' in detail
        ? String((detail as Record<string, unknown>).detail)
        : err.message
    return { message, status, detail }
  }
  if (err instanceof Error) {
    return { message: err.message, status: null, detail: err }
  }
  return { message: String(err), status: null, detail: err }
}

// ─── Paginated response wrapper ───────────────────────────────────────────────

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

// ─── Workflow endpoints ───────────────────────────────────────────────────────

export interface ListWorkflowsParams {
  status?: string
  project?: string
  owner_agent_id?: string
  tag?: string
  limit?: number
  offset?: number
}

export async function listWorkflows(
  params: ListWorkflowsParams = {}
): Promise<PaginatedResponse<Workflow>> {
  const res: AxiosResponse<PaginatedResponse<Workflow>> = await apiClient.get(
    '/workflows',
    { params: { limit: 50, offset: 0, ...params } }
  )
  return res.data
}

export async function getWorkflow(workflowId: string): Promise<Workflow> {
  const res: AxiosResponse<Workflow> = await apiClient.get(
    `/workflows/${workflowId}`
  )
  return res.data
}

export interface CreateWorkflowBody {
  name: string
  definition?: Record<string, unknown>
  trigger?: string
  owner_agent_id?: string
  project?: string
  inputs?: Record<string, unknown>
  tags?: string[]
  workflow_id?: string
}

export async function createWorkflow(
  body: CreateWorkflowBody
): Promise<Workflow> {
  const res: AxiosResponse<Workflow> = await apiClient.post('/workflows', body)
  return res.data
}

export async function cancelWorkflow(
  workflowId: string,
  reason?: string
): Promise<Workflow> {
  const res: AxiosResponse<Workflow> = await apiClient.post(
    `/workflows/${workflowId}/cancel`,
    { reason }
  )
  return res.data
}

export async function pauseWorkflow(
  workflowId: string,
  reason?: string
): Promise<Workflow> {
  const res: AxiosResponse<Workflow> = await apiClient.post(
    `/workflows/${workflowId}/pause`,
    { reason }
  )
  return res.data
}

export async function resumeWorkflow(workflowId: string): Promise<Workflow> {
  const res: AxiosResponse<Workflow> = await apiClient.post(
    `/workflows/${workflowId}/resume`,
    {}
  )
  return res.data
}

// ─── Task endpoints ───────────────────────────────────────────────────────────

export interface ListTasksParams {
  workflow_id?: string
  status?: string
  assigned_agent_id?: string
  task_type?: string
  limit?: number
  offset?: number
}

export async function listTasks(
  params: ListTasksParams = {}
): Promise<PaginatedResponse<Task>> {
  const res: AxiosResponse<PaginatedResponse<Task>> = await apiClient.get(
    '/tasks',
    { params: { limit: 100, offset: 0, ...params } }
  )
  return res.data
}

export async function getTask(taskId: string): Promise<Task> {
  const res: AxiosResponse<Task> = await apiClient.get(`/tasks/${taskId}`)
  return res.data
}

export async function listWorkflowTasks(
  workflowId: string
): Promise<PaginatedResponse<Task>> {
  const res: AxiosResponse<PaginatedResponse<Task>> = await apiClient.get(
    `/workflows/${workflowId}/tasks`,
    { params: { limit: 200, offset: 0 } }
  )
  return res.data
}

// ─── Event endpoints ──────────────────────────────────────────────────────────

export interface ListEventsParams {
  workflow_id?: string
  task_id?: string
  agent_id?: string
  event_type?: string
  topic?: string
  limit?: number
  offset?: number
}

export async function listEvents(
  params: ListEventsParams = {}
): Promise<PaginatedResponse<FlowEvent>> {
  const res: AxiosResponse<PaginatedResponse<FlowEvent>> = await apiClient.get(
    '/events',
    { params: { limit: 100, offset: 0, ...params } }
  )
  return res.data
}

export async function listWorkflowEvents(
  workflowId: string
): Promise<PaginatedResponse<FlowEvent>> {
  const res: AxiosResponse<PaginatedResponse<FlowEvent>> = await apiClient.get(
    `/workflows/${workflowId}/events`,
    { params: { limit: 200, offset: 0 } }
  )
  return res.data
}

// ─── Agent endpoints ──────────────────────────────────────────────────────────

export async function listAgents(): Promise<PaginatedResponse<Agent>> {
  const res: AxiosResponse<PaginatedResponse<Agent>> = await apiClient.get(
    '/agents',
    { params: { limit: 100, offset: 0 } }
  )
  return res.data
}

// ─── Health check ─────────────────────────────────────────────────────────────

export async function healthCheck(): Promise<{ status: string }> {
  const res: AxiosResponse<{ status: string }> = await apiClient.get('/health')
  return res.data
}
