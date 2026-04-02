"""
cli/commands — FlowOS CLI command sub-packages.

Each module in this package provides a Click command group that is registered
on the root ``flowos`` CLI:

    workflow  — Start, stop, pause, resume, and inspect workflows
    work      — Accept, update, complete, and hand off tasks (human-oriented)
    workspace — Manage Git-backed agent workspaces
    run       — Machine-oriented build/deploy execution commands
    ai        — AI-agent reasoning, proposal, and patch commands
"""
