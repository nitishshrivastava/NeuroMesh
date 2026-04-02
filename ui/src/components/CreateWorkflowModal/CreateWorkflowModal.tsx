/**
 * CreateWorkflowModal.tsx — FlowOS Create Workflow Dialog
 *
 * A dark-themed modal dialog that lets users create a new workflow by
 * providing a name, optional project/namespace, trigger type, optional
 * YAML DSL definition, and optional tags.
 *
 * On successful creation the new workflow is:
 *   1. Upserted into the Zustand store (prepended to the list)
 *   2. Selected as the active workflow
 *   3. The modal is closed
 *
 * Usage:
 *   <CreateWorkflowModal open={show} onClose={() => setShow(false)} />
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import {
  X,
  Plus,
  ChevronDown,
  ChevronRight,
  Loader2,
  AlertCircle,
  GitBranch,
} from 'lucide-react'
import { createWorkflow, normaliseError } from '../../api/client'
import { useWorkflowStore } from '../../store/workflowStore'

// ─── Types ────────────────────────────────────────────────────────────────────

type TriggerType = 'API' | 'CLI' | 'SCHEDULE' | 'WEBHOOK'

interface FormState {
  name: string
  project: string
  trigger: TriggerType
  dsl: string
  tags: string
}

interface FormErrors {
  name?: string
  dsl?: string
}

// ─── Constants ────────────────────────────────────────────────────────────────

const TRIGGER_OPTIONS: { value: TriggerType; label: string; description: string }[] = [
  { value: 'API', label: 'API', description: 'Triggered via REST API call' },
  { value: 'CLI', label: 'CLI', description: 'Triggered from command line' },
  { value: 'SCHEDULE', label: 'Schedule', description: 'Triggered on a cron schedule' },
  { value: 'WEBHOOK', label: 'Webhook', description: 'Triggered by incoming webhook' },
]

const EXAMPLE_DSL = `# Workflow DSL (YAML)
steps:
  - id: step_1
    name: "Fetch Data"
    type: agent_task
    agent_type: data_fetcher

  - id: step_2
    name: "Process Data"
    type: agent_task
    agent_type: processor
    depends_on: [step_1]
`

// ─── Styles ───────────────────────────────────────────────────────────────────

const styles = {
  overlay: {
    position: 'fixed' as const,
    inset: 0,
    background: 'rgba(0, 0, 0, 0.7)',
    backdropFilter: 'blur(4px)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 1000,
    padding: '16px',
  },
  modal: {
    background: '#1a1d27',
    border: '1px solid #2e3347',
    borderRadius: 10,
    width: '100%',
    maxWidth: 520,
    maxHeight: '90vh',
    display: 'flex',
    flexDirection: 'column' as const,
    boxShadow: '0 24px 64px rgba(0,0,0,0.6)',
    overflow: 'hidden',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '16px 20px',
    borderBottom: '1px solid #2e3347',
    flexShrink: 0,
  },
  headerLeft: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
  },
  headerTitle: {
    fontSize: 15,
    fontWeight: 700,
    color: '#e2e8f0',
    letterSpacing: '-0.01em',
  },
  closeButton: {
    background: 'transparent',
    border: 'none',
    cursor: 'pointer',
    color: '#6b7280',
    padding: 4,
    borderRadius: 4,
    display: 'flex',
    alignItems: 'center',
    transition: 'color 0.1s ease',
  },
  body: {
    flex: 1,
    overflowY: 'auto' as const,
    padding: '20px',
    display: 'flex',
    flexDirection: 'column' as const,
    gap: 16,
  },
  fieldGroup: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: 6,
  },
  label: {
    fontSize: 11,
    fontWeight: 600,
    color: '#94a3b8',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.06em',
  },
  requiredStar: {
    color: '#f87171',
    marginLeft: 2,
  },
  input: {
    background: '#0f1117',
    border: '1px solid #2e3347',
    borderRadius: 6,
    padding: '8px 12px',
    fontSize: 13,
    color: '#e2e8f0',
    outline: 'none',
    width: '100%',
    boxSizing: 'border-box' as const,
    transition: 'border-color 0.15s ease',
    fontFamily: 'inherit',
  },
  inputError: {
    borderColor: '#ef4444',
  },
  errorText: {
    fontSize: 11,
    color: '#f87171',
    display: 'flex',
    alignItems: 'center',
    gap: 4,
  },
  triggerGrid: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 8,
  },
  triggerOption: (isSelected: boolean) => ({
    background: isSelected ? 'rgba(99,102,241,0.15)' : '#0f1117',
    border: isSelected ? '1px solid rgba(99,102,241,0.5)' : '1px solid #2e3347',
    borderRadius: 6,
    padding: '8px 12px',
    cursor: 'pointer',
    textAlign: 'left' as const,
    transition: 'all 0.1s ease',
  }),
  triggerLabel: (isSelected: boolean) => ({
    fontSize: 12,
    fontWeight: 600,
    color: isSelected ? '#818cf8' : '#e2e8f0',
    display: 'block',
  }),
  triggerDesc: {
    fontSize: 10,
    color: '#6b7280',
    marginTop: 2,
    display: 'block',
  },
  collapsibleHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    cursor: 'pointer',
    background: 'transparent',
    border: 'none',
    padding: 0,
    color: '#94a3b8',
    fontSize: 11,
    fontWeight: 600,
    textTransform: 'uppercase' as const,
    letterSpacing: '0.06em',
    width: '100%',
    textAlign: 'left' as const,
  },
  textarea: {
    background: '#0f1117',
    border: '1px solid #2e3347',
    borderRadius: 6,
    padding: '10px 12px',
    fontSize: 12,
    color: '#e2e8f0',
    outline: 'none',
    width: '100%',
    boxSizing: 'border-box' as const,
    resize: 'vertical' as const,
    fontFamily: '"Fira Code", "Cascadia Code", "JetBrains Mono", monospace',
    lineHeight: 1.6,
    minHeight: 160,
    transition: 'border-color 0.15s ease',
  },
  footer: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'flex-end',
    gap: 10,
    padding: '14px 20px',
    borderTop: '1px solid #2e3347',
    flexShrink: 0,
  },
  cancelButton: {
    background: 'transparent',
    border: '1px solid #2e3347',
    borderRadius: 6,
    padding: '7px 16px',
    fontSize: 12,
    fontWeight: 600,
    color: '#6b7280',
    cursor: 'pointer',
    transition: 'all 0.1s ease',
  },
  submitButton: (disabled: boolean) => ({
    background: disabled ? 'rgba(99,102,241,0.3)' : 'rgba(99,102,241,0.9)',
    border: '1px solid rgba(99,102,241,0.6)',
    borderRadius: 6,
    padding: '7px 16px',
    fontSize: 12,
    fontWeight: 700,
    color: disabled ? '#818cf8' : '#ffffff',
    cursor: disabled ? 'not-allowed' : 'pointer',
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    transition: 'all 0.1s ease',
  }),
  globalError: {
    background: 'rgba(239,68,68,0.08)',
    border: '1px solid rgba(239,68,68,0.25)',
    borderRadius: 6,
    padding: '10px 12px',
    display: 'flex',
    alignItems: 'flex-start',
    gap: 8,
    fontSize: 12,
    color: '#f87171',
  },
  hint: {
    fontSize: 10,
    color: '#4b5563',
    marginTop: 2,
  },
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Parse a YAML-like DSL string into a definition object.
 * We do a best-effort parse: if it looks like valid YAML structure,
 * we store it as { raw: string }. A real implementation would use
 * js-yaml, but we keep zero extra deps here.
 */
function parseDsl(dsl: string): Record<string, unknown> | undefined {
  const trimmed = dsl.trim()
  if (!trimmed) return undefined
  // Store raw DSL for the backend to parse
  return { raw: trimmed }
}

/**
 * Parse comma-separated tags string into a string array.
 */
function parseTags(tags: string): string[] {
  return tags
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}

// ─── Component ────────────────────────────────────────────────────────────────

export interface CreateWorkflowModalProps {
  /** Whether the modal is visible */
  open: boolean
  /** Called when the modal should close (cancel or after successful creation) */
  onClose: () => void
}

export function CreateWorkflowModal({ open, onClose }: CreateWorkflowModalProps) {
  const upsertWorkflow = useWorkflowStore((s) => s.upsertWorkflow)
  const selectWorkflow = useWorkflowStore((s) => s.selectWorkflow)

  const [form, setForm] = useState<FormState>({
    name: '',
    project: '',
    trigger: 'API',
    dsl: '',
    tags: '',
  })
  const [errors, setErrors] = useState<FormErrors>({})
  const [globalError, setGlobalError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [dslExpanded, setDslExpanded] = useState(false)

  const nameInputRef = useRef<HTMLInputElement>(null)

  // Focus name input when modal opens; reset form when it closes
  useEffect(() => {
    if (open) {
      setForm({ name: '', project: '', trigger: 'API', dsl: '', tags: '' })
      setErrors({})
      setGlobalError(null)
      setSubmitting(false)
      setDslExpanded(false)
      // Small delay to allow the DOM to render before focusing
      setTimeout(() => nameInputRef.current?.focus(), 50)
    }
  }, [open])

  // Close on Escape key
  useEffect(() => {
    if (!open) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [open, submitting, onClose])

  // ── Validation ──────────────────────────────────────────────────────────────

  const validate = useCallback((): boolean => {
    const newErrors: FormErrors = {}

    if (!form.name.trim()) {
      newErrors.name = 'Workflow name is required'
    } else if (form.name.trim().length < 1) {
      newErrors.name = 'Workflow name must be at least 1 character'
    } else if (form.name.trim().length > 200) {
      newErrors.name = 'Workflow name must be 200 characters or fewer'
    }

    setErrors(newErrors)
    return Object.keys(newErrors).length === 0
  }, [form.name])

  // ── Submit ──────────────────────────────────────────────────────────────────

  const handleSubmit = useCallback(async () => {
    if (!validate()) return

    setGlobalError(null)
    setSubmitting(true)

    try {
      const definition = parseDsl(form.dsl)
      const tags = parseTags(form.tags)

      const newWorkflow = await createWorkflow({
        name: form.name.trim(),
        project: form.project.trim() || undefined,
        trigger: form.trigger,
        definition,
        tags: tags.length > 0 ? tags : undefined,
      })

      // Add to store and select it
      upsertWorkflow(newWorkflow)
      selectWorkflow(newWorkflow.workflow_id)

      onClose()
    } catch (err) {
      const apiErr = normaliseError(err)
      setGlobalError(apiErr.message)
    } finally {
      setSubmitting(false)
    }
  }, [form, validate, upsertWorkflow, selectWorkflow, onClose])

  // ── Field handlers ──────────────────────────────────────────────────────────

  const handleNameChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setForm((prev) => ({ ...prev, name: e.target.value }))
      if (errors.name) setErrors((prev) => ({ ...prev, name: undefined }))
    },
    [errors.name]
  )

  const handleProjectChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setForm((prev) => ({ ...prev, project: e.target.value }))
    },
    []
  )

  const handleTriggerChange = useCallback((trigger: TriggerType) => {
    setForm((prev) => ({ ...prev, trigger }))
  }, [])

  const handleDslChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setForm((prev) => ({ ...prev, dsl: e.target.value }))
      if (errors.dsl) setErrors((prev) => ({ ...prev, dsl: undefined }))
    },
    [errors.dsl]
  )

  const handleTagsChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setForm((prev) => ({ ...prev, tags: e.target.value }))
    },
    []
  )

  const handleOverlayClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget && !submitting) onClose()
    },
    [submitting, onClose]
  )

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        handleSubmit()
      }
    },
    [handleSubmit]
  )

  // ── Render ──────────────────────────────────────────────────────────────────

  if (!open) return null

  return (
    <div style={styles.overlay} onClick={handleOverlayClick} role="dialog" aria-modal="true" aria-labelledby="create-workflow-title">
      <div style={styles.modal} onKeyDown={handleKeyDown}>
        {/* ── Header ── */}
        <div style={styles.header}>
          <div style={styles.headerLeft}>
            <GitBranch size={16} color="#6366f1" />
            <span id="create-workflow-title" style={styles.headerTitle}>
              New Workflow
            </span>
          </div>
          <button
            onClick={onClose}
            disabled={submitting}
            style={styles.closeButton}
            title="Close"
            aria-label="Close modal"
            onMouseEnter={(e) => {
              ;(e.currentTarget as HTMLButtonElement).style.color = '#e2e8f0'
            }}
            onMouseLeave={(e) => {
              ;(e.currentTarget as HTMLButtonElement).style.color = '#6b7280'
            }}
          >
            <X size={16} />
          </button>
        </div>

        {/* ── Body ── */}
        <div style={styles.body}>
          {/* Global error banner */}
          {globalError && (
            <div style={styles.globalError} role="alert">
              <AlertCircle size={14} style={{ flexShrink: 0, marginTop: 1 }} />
              <span>{globalError}</span>
            </div>
          )}

          {/* Workflow Name */}
          <div style={styles.fieldGroup}>
            <label htmlFor="wf-name" style={styles.label}>
              Workflow Name<span style={styles.requiredStar}>*</span>
            </label>
            <input
              id="wf-name"
              ref={nameInputRef}
              type="text"
              value={form.name}
              onChange={handleNameChange}
              placeholder="e.g. data-pipeline, model-training-v2"
              disabled={submitting}
              autoComplete="off"
              spellCheck={false}
              style={{
                ...styles.input,
                ...(errors.name ? styles.inputError : {}),
              }}
              onFocus={(e) => {
                if (!errors.name)
                  (e.currentTarget as HTMLInputElement).style.borderColor = '#6366f1'
              }}
              onBlur={(e) => {
                if (!errors.name)
                  (e.currentTarget as HTMLInputElement).style.borderColor = '#2e3347'
              }}
            />
            {errors.name && (
              <span style={styles.errorText}>
                <AlertCircle size={11} />
                {errors.name}
              </span>
            )}
          </div>

          {/* Project / Namespace */}
          <div style={styles.fieldGroup}>
            <label htmlFor="wf-project" style={styles.label}>
              Project / Namespace
            </label>
            <input
              id="wf-project"
              type="text"
              value={form.project}
              onChange={handleProjectChange}
              placeholder="e.g. core, ml-team, infra (optional)"
              disabled={submitting}
              autoComplete="off"
              spellCheck={false}
              style={styles.input}
              onFocus={(e) => {
                ;(e.currentTarget as HTMLInputElement).style.borderColor = '#6366f1'
              }}
              onBlur={(e) => {
                ;(e.currentTarget as HTMLInputElement).style.borderColor = '#2e3347'
              }}
            />
            <span style={styles.hint}>
              Groups workflows by team or project namespace
            </span>
          </div>

          {/* Trigger Type */}
          <div style={styles.fieldGroup}>
            <span style={styles.label}>Trigger</span>
            <div style={styles.triggerGrid}>
              {TRIGGER_OPTIONS.map((opt) => {
                const isSelected = form.trigger === opt.value
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => handleTriggerChange(opt.value)}
                    disabled={submitting}
                    style={styles.triggerOption(isSelected)}
                    onMouseEnter={(e) => {
                      if (!isSelected)
                        (e.currentTarget as HTMLButtonElement).style.borderColor =
                          'rgba(99,102,241,0.3)'
                    }}
                    onMouseLeave={(e) => {
                      if (!isSelected)
                        (e.currentTarget as HTMLButtonElement).style.borderColor =
                          '#2e3347'
                    }}
                  >
                    <span style={styles.triggerLabel(isSelected)}>
                      {opt.label}
                    </span>
                    <span style={styles.triggerDesc}>{opt.description}</span>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Workflow DSL — collapsible */}
          <div style={styles.fieldGroup}>
            <button
              type="button"
              onClick={() => setDslExpanded((v) => !v)}
              style={styles.collapsibleHeader}
              aria-expanded={dslExpanded}
            >
              {dslExpanded ? (
                <ChevronDown size={12} />
              ) : (
                <ChevronRight size={12} />
              )}
              Workflow DSL
              <span
                style={{
                  fontSize: 9,
                  color: '#4b5563',
                  fontWeight: 400,
                  textTransform: 'none',
                  letterSpacing: 0,
                  marginLeft: 4,
                }}
              >
                (optional YAML)
              </span>
            </button>

            {dslExpanded && (
              <div style={{ marginTop: 6 }}>
                <textarea
                  id="wf-dsl"
                  value={form.dsl}
                  onChange={handleDslChange}
                  placeholder={EXAMPLE_DSL}
                  disabled={submitting}
                  spellCheck={false}
                  style={{
                    ...styles.textarea,
                    ...(errors.dsl ? styles.inputError : {}),
                  }}
                  onFocus={(e) => {
                    if (!errors.dsl)
                      (e.currentTarget as HTMLTextAreaElement).style.borderColor =
                        '#6366f1'
                  }}
                  onBlur={(e) => {
                    if (!errors.dsl)
                      (e.currentTarget as HTMLTextAreaElement).style.borderColor =
                        '#2e3347'
                  }}
                />
                {errors.dsl && (
                  <span style={{ ...styles.errorText, marginTop: 4 }}>
                    <AlertCircle size={11} />
                    {errors.dsl}
                  </span>
                )}
                <span style={styles.hint}>
                  Paste YAML workflow definition. Leave blank to create an empty
                  workflow.
                </span>
              </div>
            )}
          </div>

          {/* Tags */}
          <div style={styles.fieldGroup}>
            <label htmlFor="wf-tags" style={styles.label}>
              Tags
            </label>
            <input
              id="wf-tags"
              type="text"
              value={form.tags}
              onChange={handleTagsChange}
              placeholder="e.g. production, nightly, ml (comma-separated)"
              disabled={submitting}
              autoComplete="off"
              spellCheck={false}
              style={styles.input}
              onFocus={(e) => {
                ;(e.currentTarget as HTMLInputElement).style.borderColor = '#6366f1'
              }}
              onBlur={(e) => {
                ;(e.currentTarget as HTMLInputElement).style.borderColor = '#2e3347'
              }}
            />
            <span style={styles.hint}>
              Separate multiple tags with commas
            </span>
          </div>
        </div>

        {/* ── Footer ── */}
        <div style={styles.footer}>
          <span style={{ fontSize: 10, color: '#4b5563', marginRight: 'auto' }}>
            {/* Keyboard shortcut hint */}
            ⌘↵ to create
          </span>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            style={styles.cancelButton}
            onMouseEnter={(e) => {
              ;(e.currentTarget as HTMLButtonElement).style.borderColor = '#4b5563'
              ;(e.currentTarget as HTMLButtonElement).style.color = '#94a3b8'
            }}
            onMouseLeave={(e) => {
              ;(e.currentTarget as HTMLButtonElement).style.borderColor = '#2e3347'
              ;(e.currentTarget as HTMLButtonElement).style.color = '#6b7280'
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={submitting || !form.name.trim()}
            style={styles.submitButton(submitting || !form.name.trim())}
            onMouseEnter={(e) => {
              if (!submitting && form.name.trim())
                (e.currentTarget as HTMLButtonElement).style.background =
                  'rgba(99,102,241,1)'
            }}
            onMouseLeave={(e) => {
              if (!submitting && form.name.trim())
                (e.currentTarget as HTMLButtonElement).style.background =
                  'rgba(99,102,241,0.9)'
            }}
          >
            {submitting ? (
              <>
                <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />
                Creating…
              </>
            ) : (
              <>
                <Plus size={13} />
                Create Workflow
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
