"""
orchestrator — FlowOS Temporal Orchestrator Package

This package contains the Temporal worker, workflow definitions, activity
implementations, and DSL parser for the FlowOS orchestration engine.

The orchestrator is the durable execution backbone of FlowOS.  It:
- Parses YAML workflow DSL into executable Temporal workflows
- Executes workflows as durable Temporal workflow functions
- Dispatches tasks to agents via Kafka events
- Tracks workflow and task state in PostgreSQL
- Handles checkpoints, handoffs, and reversions
"""
