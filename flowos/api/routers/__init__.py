"""
api/routers/__init__.py — FlowOS API Router Package

Exports all router instances for registration in main.py.
"""

from api.routers.workflows import router as workflows_router
from api.routers.tasks import router as tasks_router
from api.routers.agents import router as agents_router
from api.routers.checkpoints import router as checkpoints_router
from api.routers.handoffs import router as handoffs_router
from api.routers.events import router as events_router

__all__ = [
    "workflows_router",
    "tasks_router",
    "agents_router",
    "checkpoints_router",
    "handoffs_router",
    "events_router",
]
