"""
api/__init__.py — FlowOS REST API Package

Exposes the FastAPI application factory and WebSocket server for import.
"""

from api.main import create_app

__all__ = ["create_app"]
