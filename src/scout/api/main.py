"""Entrypoint alias for uvicorn running scout.api.main:app."""

from scout.api.app import app

__all__ = ["app"]
