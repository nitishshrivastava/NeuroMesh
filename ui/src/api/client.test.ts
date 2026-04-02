/**
 * client.test.ts — Unit tests for the FlowOS API client utilities
 *
 * Tests cover:
 * - normaliseError(): AxiosError, generic Error, unknown values
 * - PaginatedResponse shape
 * - apiClient base URL configuration
 */

import { describe, it, expect } from 'vitest'
import { AxiosError } from 'axios'
import { normaliseError, apiClient } from './client'
import type { ApiError, PaginatedResponse } from './client'
import type { Workflow } from '../store/workflowStore'

// ---------------------------------------------------------------------------
// normaliseError tests
// ---------------------------------------------------------------------------

describe('normaliseError', () => {
  it('extracts message and status from AxiosError with response', () => {
    const axiosErr = new AxiosError('Request failed')
    axiosErr.response = {
      status: 404,
      data: { detail: 'Workflow not found' },
      statusText: 'Not Found',
      headers: {},
      config: {} as any,
    }

    const result: ApiError = normaliseError(axiosErr)

    expect(result.status).toBe(404)
    expect(result.message).toBe('Workflow not found')
    expect(result.detail).toEqual({ detail: 'Workflow not found' })
  })

  it('returns null status when AxiosError has no response', () => {
    const axiosErr = new AxiosError('Network Error')
    // No response set — simulates a network timeout

    const result: ApiError = normaliseError(axiosErr)

    expect(result.status).toBeNull()
    expect(result.message).toBe('Network Error')
  })

  it('handles AxiosError with non-object detail in response data', () => {
    const axiosErr = new AxiosError('Server Error')
    axiosErr.response = {
      status: 500,
      data: 'Internal Server Error',
      statusText: 'Internal Server Error',
      headers: {},
      config: {} as any,
    }

    const result: ApiError = normaliseError(axiosErr)

    expect(result.status).toBe(500)
    // Falls back to err.message when data is not an object with 'detail'
    expect(result.message).toBe('Server Error')
  })

  it('handles generic Error instances', () => {
    const err = new Error('Something went wrong')

    const result: ApiError = normaliseError(err)

    expect(result.status).toBeNull()
    expect(result.message).toBe('Something went wrong')
    expect(result.detail).toBe(err)
  })

  it('handles string errors', () => {
    const result: ApiError = normaliseError('plain string error')

    expect(result.status).toBeNull()
    expect(result.message).toBe('plain string error')
    expect(result.detail).toBe('plain string error')
  })

  it('handles null/undefined errors gracefully', () => {
    const resultNull: ApiError = normaliseError(null)
    expect(resultNull.message).toBe('null')
    expect(resultNull.status).toBeNull()

    const resultUndefined: ApiError = normaliseError(undefined)
    expect(resultUndefined.message).toBe('undefined')
    expect(resultUndefined.status).toBeNull()
  })

  it('handles AxiosError with 422 validation error detail (array)', () => {
    const axiosErr = new AxiosError('Unprocessable Entity')
    const validationErrors = [{ loc: ['body', 'name'], msg: 'field required', type: 'value_error.missing' }]
    axiosErr.response = {
      status: 422,
      data: { detail: validationErrors },
      statusText: 'Unprocessable Entity',
      headers: {},
      config: {} as any,
    }

    const result: ApiError = normaliseError(axiosErr)

    expect(result.status).toBe(422)
    // When detail is an array, String(array) is called — result is a string
    expect(typeof result.message).toBe('string')
    // The response data is preserved in detail
    expect(result.detail).toEqual({ detail: validationErrors })
  })

  it('handles AxiosError with 409 conflict detail', () => {
    const axiosErr = new AxiosError('Conflict')
    axiosErr.response = {
      status: 409,
      data: { detail: 'Workflow with ID "abc" already exists.' },
      statusText: 'Conflict',
      headers: {},
      config: {} as any,
    }

    const result: ApiError = normaliseError(axiosErr)

    expect(result.status).toBe(409)
    expect(result.message).toBe('Workflow with ID "abc" already exists.')
  })
})

// ---------------------------------------------------------------------------
// apiClient configuration tests
// ---------------------------------------------------------------------------

describe('apiClient', () => {
  it('has correct default timeout of 15 seconds', () => {
    expect(apiClient.defaults.timeout).toBe(15_000)
  })

  it('sends JSON content-type header', () => {
    const contentType = apiClient.defaults.headers['Content-Type']
    expect(contentType).toBe('application/json')
  })

  it('sends Accept: application/json header', () => {
    const accept = apiClient.defaults.headers['Accept']
    expect(accept).toBe('application/json')
  })
})

// ---------------------------------------------------------------------------
// PaginatedResponse type shape test (compile-time + runtime)
// ---------------------------------------------------------------------------

describe('PaginatedResponse shape', () => {
  it('accepts a valid paginated workflow response', () => {
    const mockWorkflow: Workflow = {
      workflow_id: '550e8400-e29b-41d4-a716-446655440000',
      name: 'test-workflow',
      status: 'pending',
      trigger: 'api',
      definition: null,
      temporal_run_id: null,
      temporal_workflow_id: null,
      owner_agent_id: null,
      project: 'test-project',
      inputs: {},
      outputs: {},
      error_message: null,
      error_details: {},
      tags: ['test'],
      created_at: '2026-01-01T00:00:00Z',
      started_at: null,
      completed_at: null,
      updated_at: '2026-01-01T00:00:00Z',
    }

    const response: PaginatedResponse<Workflow> = {
      items: [mockWorkflow],
      total: 1,
      limit: 50,
      offset: 0,
    }

    expect(response.items).toHaveLength(1)
    expect(response.total).toBe(1)
    expect(response.limit).toBe(50)
    expect(response.offset).toBe(0)
    expect(response.items[0].workflow_id).toBe('550e8400-e29b-41d4-a716-446655440000')
    expect(response.items[0].name).toBe('test-workflow')
    expect(response.items[0].status).toBe('pending')
  })

  it('accepts an empty paginated response', () => {
    const response: PaginatedResponse<Workflow> = {
      items: [],
      total: 0,
      limit: 50,
      offset: 0,
    }

    expect(response.items).toHaveLength(0)
    expect(response.total).toBe(0)
  })

  it('correctly types a multi-page response', () => {
    const response: PaginatedResponse<Workflow> = {
      items: [],
      total: 150,
      limit: 50,
      offset: 50,
    }

    // Page 2 of 3
    expect(response.total).toBe(150)
    expect(response.offset).toBe(50)
    expect(response.limit).toBe(50)
  })
})
