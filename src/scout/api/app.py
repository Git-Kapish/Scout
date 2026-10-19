"""Scout FastAPI Application."""

from fastapi import FastAPI

from scout.api.health import router as health_router

app = FastAPI(
    title="Scout Research Assistant API",
    description="Durable, Budgeted, Traceable Multi-Agent Research Service",
    version="0.1.0",
)

app.include_router(health_router)
