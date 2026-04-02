# FlowOS — Exploration Notes for Planning Agent

## 1. Project Overview

**Project Name:** FlowOS — Distributed Human + Machine Orchestration Platform  
**Source Document:** `/Users/nitishshrivastava/Documents/sasva/NeuroMesh/flowos_full_design_v2.md` (29,260 bytes, ~1,310 lines)  
**Status:** Greenfield — design document only, no code exists yet  
**Nature:** This is a comprehensive architectural design specification for a brand-new distributed system. There is no existing codebase to integrate with. The task is to implement this system from scratch based on the design.

---

## 2. Directory Structure

```
/Users/nitishshrivastava/Documents/sasva/NeuroMesh/
└── flowos_full_design_v2.md   (the only file — pure design doc)
```

No source code, no config files, no tests, no CI/CD, no package manifests exist yet. This is a pure greenfield build.

---

## 3. Design Document Structure (26 Sections)

The design document covers 26 major sections:

| Section | Title |
|---------|-------|
| 1 | Vision |
| 2 | Core Principles (7 sub-principles) |
| 3 | What This System Is Closest To |
| 4 | High-Level Architecture |
| 5 | Major System Layers (8 sub-layers) |
| 6 | Local Git Per Node (5 sub-sections) |
| 7 | Workflow Model |
| 8 | Domain Models (8 entities) |
| 9 | CLI and Workspace Operations |
| 10 | Checkpoint Model |
| 11 | Revert and Resume Semantics (4 types) |
| 12 | Handoff Design |
| 13 | Git Strategy |
| 14 | Snapshot Model |
| 15 | Machine Node Safety Rules |
| 16 | Orchestrator Responsibilities with Git-Aware Nodes |
| 17 | Workflow Definition DSL |
| 18 | Recommended Technology Stack |
| 19 | LangChain and LangGraph Integration Patterns (3 patterns) |
| 20 | Observability |
| 21 | Security and Policy |
| 22 | MVP Plan (3 phases) |
| 23 | Future Extensions |
| 24 | Why This Design Is Strong |
| 25 | Final Design Principle |
| 26 | One-Line Definition |

---

## 4. Architecture Overview

### One-Line Definition
> FlowOS is a durable event-driven orchestration layer where humans, machines, and AI agents collaborate through stateful workflows, every node maintains a local Git-backed reversible workspace, and AI reasoning frameworks such as LangChain and LangGraph plug in as modular execution layers.

### Mental Model
> FlowOS = Temporal + event bus + Git-backed node workspaces + LangGraph-compatible reasoning + remote execution + collaborative handoff

### Core Principles
1. **Everything is an Agent** — Human → CLI agent, Machine → execution agent, AI → reasoning agent
2. **Everything is Event-Driven** — No polling, no tight coupling, all actions emit/consume events
3. **Workflows Are Durable** — Survive crashes, resume anywhere, full history replayable
4. **State Is First-Class** — Persistent workflow state graph, not just logs
5. **Ownership Is Transferable** — Tasks can move between users, machines, and AI actors
6. **Every Node Owns a Revertible Workspace** — Local Git-backed workspace per node
7. **AI Frameworks Are Pluggable, Not Foundational** — LangChain/LangGraph are execution modules, not the system of record

### High-Level Architecture (7 layers)
```
[ User CLI / Machine CLI / AI CLI ]
         ↓
[ Local Workspace Manager ]
         ↓
[ Event Bus ]
         ↓
[ Orchestrator / Workflow Brain ]
         ↓
[ Human Tasks | Machine Tasks | AI Reasoning | Policy Engine | Observability ]
         ↓
[ Execution Layer: K8s / Argo / Remote Workers ]
         ↓
[ External Systems: Git, Build, Test, Artifact Store, Secrets, APIs ]
```

---

## 5. Major System Layers (Detailed)

### 5.1 Orchestrator Layer
- **Recommended base:** Temporal
- **Responsibilities:** Workflow lifecycle, durable execution, waiting for signals, ownership transfer, retry management, failure recovery, replay and audit
- **Owns:** workflow truth, execution history, retries, timers, pause/resume, human-in-the-loop waiting, cross-node handoff state
- **Critical rule:** LangGraph/LangChain must NOT replace this layer

### 5.2 Event Bus Layer
- **Options:** NATS (lightweight, low-latency), Kafka (enterprise scale, replay), Redis Streams (small setups only)
- **Event types defined:** WORKFLOW_CREATED, WORKFLOW_PAUSED, WORKFLOW_RESUMED, TASK_CREATED, TASK_ASSIGNED, TASK_ACCEPTED, TASK_CHECKPOINTED, TASK_COMPLETED, TASK_FAILED, TASK_REVERT_REQUESTED, TASK_HANDOFF_REQUESTED, BUILD_TRIGGERED, BUILD_SUCCEEDED, BUILD_FAILED, TEST_STARTED, TEST_FINISHED, USER_INPUT_RECEIVED, AI_SUGGESTION_CREATED, POLICY_VIOLATION_DETECTED

### 5.3 CLI Agent Layer
- **Node types:** Human workstation, Remote build machine, Test machine, Autonomous AI execution node, Review node, Utility node
- **Human CLI examples:** `flowos work list`, `flowos work accept TASK_ID`, `flowos work checkpoint TASK_ID --label "before-refactor"`, `flowos work handoff TASK_ID --to user_b`
- **Machine CLI examples:** `flowos run build --repo core-service`, `flowos run test --suite smoke`, `flowos workspace checkpoint --label "post-build"`, `flowos workspace revert --checkpoint cp_1021`
- **AI CLI examples:** `flowos ai propose-fix --task TASK_ID`, `flowos ai apply-patch --branch wf/2001/refactor-option-b`, `flowos ai summarize-handoff --task TASK_ID`

### 5.4 AI Orchestration and Reasoning Layer
- **Frameworks:** LangGraph (graph-based state transitions, planner-reviewer loops, conditional routing), LangChain (model wrappers, RAG, tool integration), AutoGen/CrewAI (conversational multi-agent, role-based)
- **Rule:** All AI frameworks execute inside FlowOS-managed task boundaries and must emit structured outputs, diffs, events, checkpoints, handoff references
- **LangGraph best for:** AI reasoning pipelines, code review agents, fix-validation loops, document analysis, research/planning nodes
- **LangChain best for:** LLM abstraction, RAG helpers, tool adapters, prompt orchestration

### 5.5 Execution Layer
- **Base:** Kubernetes + Argo Workflows/Events
- **Mental model:** Temporal = workflow brain, Argo = compute muscle, LangGraph = AI reasoning graph, Git = local reversible state
- **Responsibilities:** Build execution, test execution, static analysis, deployment tasks, artifact generation, heavy compute

### 5.6 State and Metadata Layer
- **Temporal history** — workflow execution state
- **Postgres** — structured metadata
- **Neo4j** (optional) — relationship graph and lineage
- **Object storage (S3-compatible)** — large artifacts and snapshots
- **Vector store** (optional: Qdrant, Weaviate, pgvector, Elasticsearch) — semantic recall, agent context lookup
- **Metadata stored:** Workflow definitions, Task lifecycle, Agent registry, Workspace references, Branch ownership, Checkpoint history, Handoff chain, Artifact references, Policy outcomes, Execution metrics

### 5.7 Tool and Connector Layer
- **MCP-compatible design** — standardized schemas, discoverable capabilities, typed I/O, permission/policy controls, reusable tool wrappers
- **Tools:** Git operations, build systems, test frameworks, compiler runners, issue trackers, docs systems, artifact stores, terminal commands, cloud APIs, browser automation, design systems
- **LangChain/LangGraph** can consume these tools through their native tool interfaces

### 5.8 UI Layer
- **Inspiration:** LangGraph visualization + n8n node graph + GitHub Actions run history + Cursor task context + Claude Code execution trace
- **Views:** Workflow graph, Live state by step, Agent ownership panel, Event timeline, Workspace state references, Diff and checkpoint viewer, Replay and recovery controls, AI reasoning trace viewer, Tool call trace viewer

---

## 6. Local Git Per Node (Section 6)

### Node-Level Architecture
```
[ CLI Agent ]
     ↓
[ Workspace Manager ]
     ├── repo/                      → local git checkout
     ├── .flowos/state.json         → local execution state
     ├── .flowos/checkpoints.json
     ├── .flowos/task_context.json
     ├── .flowos/agent_identity.json
     ├── .flowos/reasoning_trace.json
     ├── artifacts/
     ├── logs/
     └── patches/
```

### Workspace Root Structure
```
/flowos/workspaces/{workflow_id}/{task_id}/
├── repo/
├── .flowos/
│   ├── state.json
│   ├── checkpoints.json
│   ├── task_context.json
│   ├── agent_identity.json
│   ├── event_buffer.json
│   └── reasoning_trace.json
├── artifacts/
├── logs/
└── patches/
```

### Git Strategy
- **Branch naming:** `wf/{workflow_id}/{task_name}`
- **Tags:** `checkpoint/{checkpoint_id}`, `build/success/{build_id}`, `build/failure/{build_id}`, `handoff/{handoff_id}`, `review/approved/{task_id}`, `review/rejected/{task_id}`
- **Commit message format:** `[wf:wf_2001][task:task_feature_impl][agent:user_nitish] add retry guard to deployment pipeline`

---

## 7. Domain Models (Section 8)

### 8.1 Workflow
```json
{
  "id": "wf_2001",
  "name": "feature_delivery_pipeline",
  "status": "active",
  "current_state": "build_pending",
  "created_by": "user_nitish",
  "created_at": "2026-04-02T14:00:00Z",
  "metadata": { "project": "core-platform", "priority": "high" }
}
```

### 8.2 Task
```json
{
  "id": "task_feature_impl",
  "workflow_id": "wf_2001",
  "type": "human",
  "status": "active",
  "assigned_to": "user_nitish",
  "input": { "spec_ref": "artifact://specs/feature-a.md" },
  "output": {},
  "dependencies": ["task_design_done"],
  "metadata": { "branch": "wf/wf_2001/feature_impl" }
}
```

### 8.3 Agent
```json
{
  "id": "machine_build_07",
  "type": "machine",
  "capabilities": ["build", "unit_test", "artifact_upload"],
  "current_task": "task_build_fix",
  "status": "busy",
  "machine_info": { "os": "ubuntu-22.04", "toolchain": "gcc-14" }
}
```

### 8.4 Event
```json
{
  "id": "evt_8881",
  "type": "TASK_CHECKPOINTED",
  "source_agent": "user_nitish",
  "target": "orchestrator",
  "payload": { "workflow_id": "wf_2001", "task_id": "task_feature_impl", "checkpoint_id": "cp_302" },
  "timestamp": "2026-04-02T15:21:00Z"
}
```

### 8.5 Workspace
```json
{
  "workspace_id": "ws_2001_feature_impl_user_nitish",
  "workflow_id": "wf_2001",
  "task_id": "task_feature_impl",
  "agent_id": "user_nitish",
  "root_path": "/flowos/workspaces/wf_2001/task_feature_impl/",
  "branch": "wf/wf_2001/feature_impl",
  "head_commit": "93cd1af",
  "status": "active"
}
```

### 8.6 Checkpoint
```json
{
  "checkpoint_id": "cp_1021",
  "workflow_id": "wf_2001",
  "task_id": "task_build_fix",
  "agent_id": "machine_build_07",
  "branch": "wf/wf_2001/task_build_fix",
  "commit": "a81f2d9",
  "label": "before-compile-fix",
  "timestamp": "2026-04-02T14:35:00Z",
  "note": "Before changing linker flags"
}
```

### 8.7 Handoff
```json
{
  "handoff_id": "ho_901",
  "workflow_id": "wf_2001",
  "task_id": "task_feature_impl",
  "from_agent": "user_nitish",
  "to_agent": "machine_build_02",
  "branch": "wf/wf_2001/feature_impl",
  "commit": "93cd1af",
  "checkpoint_id": "cp_302",
  "summary": "Feature code complete. Ready for compile and unit test.",
  "open_issues": ["Verify dependency version conflict"],
  "next_action": "Run full build and regression subset"
}
```

### 8.8 Reasoning Run
```json
{
  "reasoning_run_id": "rr_501",
  "task_id": "task_ai_review",
  "framework": "langgraph",
  "graph_name": "code_review_and_fix_validation",
  "inputs": { "workspace_ref": "wf/wf_2001/feature_impl@93cd1af" },
  "outputs": { "summary": "Potential null check missing in retry path", "proposed_patch_branch": "wf/wf_2001/ai_fix_option_a" },
  "status": "completed"
}
```

---

## 8. Workflow Definition DSL (Section 17)

YAML-based declarative workflow definition:

```yaml
workflow:
  name: feature_delivery_pipeline
  metadata:
    project: core-platform
    owner: platform-team

  steps:
    - name: design
      type: human
      checkpoint_policy: required_before_complete

    - name: implement
      type: human
      depends_on: [design]
      branch: wf/{{workflow_id}}/implement

    - name: ai-review
      type: ai
      framework: langgraph
      graph: code_review_and_fix_validation
      depends_on: [implement]

    - name: build
      type: machine
      depends_on: [implement]
      runtime: argo
      artifacts:
        - build.log
        - binary.tar.gz

    - name: review
      type: human
      depends_on: [build, ai-review]

    - name: publish
      type: machine
      depends_on: [review]
```

---

## 9. MVP Plan (Section 22)

### Phase 1 (Core Foundation)
- Temporal workflows
- NATS or Kafka event bus
- CLI agent
- Local Git-backed workspace per node
- Basic task assignment
- Checkpoint and revert
- Simple handoff model

### Phase 2 (Execution + Visibility)
- Machine execution workers
- Build and test integration
- LangChain-based tool layer
- Artifact and snapshot storage
- UI visualization
- Event timeline
- Replay controls

### Phase 3 (Intelligence + Policy)
- LangGraph reasoning subgraphs
- Policy engine
- Workflow DSL
- Branch lineage visualization
- Optimization recommendations
- Learning from historical workflow outcomes

---

## 10. Recommended Technology Stack (Section 18)

| Component | Technology |
|-----------|-----------|
| Durable orchestration | Temporal |
| Event transport | NATS (simple) or Kafka (enterprise) |
| AI reasoning | LangGraph + LangChain |
| Remote execution | Kubernetes + Argo Workflows + Argo Events |
| CLI runtime | Python or Go |
| Git operations | GitPython or native git |
| Metadata store | Postgres |
| Artifact storage | S3-compatible object storage |
| Graph store (optional) | Neo4j |
| Vector store (optional) | Qdrant or pgvector |
| UI | React + React Flow + WebSockets/SSE |
| Tool contracts | MCP-like standardized schemas |

---

## 11. Observability Requirements (Section 20)

### Required Views
- Workflow timeline
- Task graph
- Ownership transitions
- Checkpoint list
- Branch lineage view
- Event stream
- Execution logs
- Artifact pointers
- Replay actions taken
- LangGraph trace
- Tool call history
- AI decision summaries

### Key Metrics
- Task cycle time
- Human waiting time
- Machine waiting time
- Checkpoint frequency
- Retry count
- Revert frequency
- Handoff success rate
- Build success rate by branch type
- AI suggestion acceptance rate

---

## 12. Security and Policy (Section 21)

### Required Controls
- Agent authentication (tokens or mTLS)
- Role-based assignment control
- Workflow-level authorization
- Branch protection policies
- Artifact access policies
- Command execution restrictions
- Approval gates for risky actions

### Policy Examples
- AI nodes cannot push directly to protected branches
- Machine nodes may build but not deploy without approval
- Human checkpoint required before release review
- Secrets access allowed only to trusted execution nodes
- LangGraph tool access restricted by task policy
- Retrieval layer may only access allowed corpora for that workflow

---

## 13. Revert and Resume Semantics (Section 11)

Four types of revert/resume:
1. **Soft local rollback** — Return working tree to prior checkpoint without changing orchestration ownership (experiment failed, bad generated code, unexpected compile break)
2. **Step-level rollback** — Revert node state to last accepted workflow checkpoint (orchestrator requests retry, reviewer rejects output)
3. **Fork-and-continue** — Return to old point, create new branch, continue from there (alternate implementation needed, multiple solutions to compare)
4. **Rehydrate on another node** — Another node restores state using commit hash, branch ref, patch bundle, or snapshot manifest (ownership transfers, machine changes, user continues elsewhere)

---

## 14. Handoff Design (Section 12)

A handoff must transfer a **recoverable working reference**, not just a status string.

Handoff payload includes:
- workflow_id, task_id
- source agent id, target agent id
- branch, commit hash
- latest checkpoint id
- summary, open issues
- artifact references
- suggested next action

---

## 15. Snapshot Model (Section 14)

Beyond commits, snapshots capture:
- Git reference
- Metadata manifest
- Artifact pointers (e.g., `s3://build-artifacts/wf_2001/test-report.xml`)
- Environment descriptor (OS, toolchain, runtime versions)

---

## 16. Machine Node Safety Rules (Section 15)

- Never commit to a shared branch directly without policy
- Use ephemeral working branches for generated changes
- Require checkpoint before destructive action
- Tag build outcomes automatically
- Attach logs to checkpoint metadata
- Clean workspace only after snapshot or handoff is persisted
- Preserve failed build state for replay when policy requires it

---

## 17. LangChain/LangGraph Integration Patterns (Section 19)

### Pattern A: LangGraph inside a FlowOS task
1. FlowOS assigns task → Agent loads workspace/context → LangGraph executes local reasoning graph → Graph produces structured output/patch → Node writes diff and checkpoint → Node emits completion event back to FlowOS

### Pattern B: LangChain as tool and retrieval layer
1. FlowOS task starts → LangChain loads retriever, tools, model wrappers → Task gets response/generated plan → Output converted into FlowOS artifacts and events

### Pattern C: LangGraph for sub-agents
- Internal AI collaboration (planner, reviewer, executor, validator roles)
- Rule: Subgraph must run under a single FlowOS-owned task or explicitly created FlowOS child tasks

---

## 18. Key Implementation Risks and Complexities

### Risk 1: Temporal Complexity
Temporal has a steep learning curve. Workflow definitions, activities, signals, and queries need careful design. The team needs Temporal expertise or significant ramp-up time.

### Risk 2: Git-per-Node Workspace Management
Managing Git workspaces across potentially hundreds of nodes (human, machine, AI) introduces complexity around: workspace isolation, branch proliferation, cleanup policies, and conflict resolution.

### Risk 3: Event Bus Choice (NATS vs Kafka)
The design leaves this open. NATS is simpler but Kafka offers better replay semantics. This decision affects the entire event-driven architecture and is hard to change later.

### Risk 4: CLI Language Choice (Python vs Go)
The design suggests Python or Go. Python has better LangChain/LangGraph ecosystem integration; Go has better performance and distribution characteristics for a CLI tool.

### Risk 5: Scope Creep
The design is extremely ambitious (26 sections, 3 MVP phases). Without clear MVP boundaries, the implementation could sprawl. The MVP plan in Section 22 provides guidance but needs further scoping.

### Risk 6: LangGraph/LangChain Version Compatibility
These frameworks evolve rapidly. The integration patterns need to be designed with version pinning and abstraction layers to avoid breaking changes.

### Risk 7: UI Complexity
The UI requirements (workflow graph, live state, event timeline, diff viewer, replay controls, AI trace viewer) are substantial. React Flow-based graph visualization with real-time updates is non-trivial.

### Risk 8: Testing Strategy
No testing strategy is defined in the design. For a system with Temporal workflows, event buses, Git operations, and AI integrations, testing is complex and needs careful planning (unit, integration, e2e, chaos testing).

### Risk 9: Deployment and Infrastructure
The design assumes Kubernetes + Argo but doesn't specify deployment topology, multi-tenancy, or how the system itself is deployed and operated.

### Risk 10: Authentication/Authorization Design
The design mentions tokens or mTLS but doesn't specify the auth system (OAuth2, JWT, API keys, etc.) or how agent identities are provisioned and managed.

---

## 19. Integration Points for New Work

Since this is greenfield, all integration points are NEW. The key integration surfaces are:

1. **Temporal ↔ Event Bus** — Temporal workflows emit events to NATS/Kafka; event bus delivers signals back to Temporal
2. **CLI Agent ↔ Temporal** — CLI agent submits workflow/task operations to Temporal via SDK
3. **CLI Agent ↔ Local Git** — CLI agent manages local workspace via GitPython or native git
4. **CLI Agent ↔ Event Bus** — CLI agent publishes/subscribes to events
5. **LangGraph/LangChain ↔ FlowOS Task** — AI frameworks execute within task boundaries, emit structured outputs
6. **Argo ↔ Temporal** — Argo executes compute jobs triggered by Temporal activities
7. **Postgres ↔ All Layers** — Metadata store for all domain entities
8. **UI ↔ Backend** — React frontend connects via WebSockets/SSE for real-time updates
9. **Tool Layer ↔ AI Frameworks** — MCP-compatible tool contracts consumed by LangChain/LangGraph

---

## 20. Future Extensions (Section 23)

- Skill-based routing of tasks
- Cost-aware compute selection
- Workspace warm-starting
- Simulation and what-if execution branches
- Multi-repository workflows
- Graph-based dependency reasoning
- Organizational knowledge memory
- Automatic checkpoint suggestions
- Intelligent branch merge planning
- Framework-neutral AI orchestration interface
- Import adapters for Claude Code or Cursor-like local runtimes
