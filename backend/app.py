"""Create the local demo control plane, configure persistence, mount the frontend, and enforce application limits.


"""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.tasks import task_router
from backend.artifacts.files import InputFiles
from backend.bootstrap import ApplicationDependencies, build_dependencies
from backend.domain.store import TaskStore


def create_app(
    data_dir: Path | None = None,
    demo_token: str | None = None,
    max_upload_bytes: int | None = None,
    dependencies: ApplicationDependencies | None = None,
) -> FastAPI:
    """Create the application with its data directory, demo token, upload limit, and trusted injected dependencies.







    """
    services = dependencies if dependencies is not None else build_dependencies()
    # Reject missing or unsafe demo token configuration.
    token = demo_token or os.environ.get("AI_AGENT_DEMO_TOKEN")
    if not token or token == "replace-with-a-long-random-secret":
        raise RuntimeError("Set the AI_AGENT_DEMO_TOKEN environment variable before starting the control plane")

    # Configure the data root.
    root = data_dir or Path(os.environ.get("AI_AGENT_DATA_DIR", ".runtime"))
    
    # Resolve the upload limit.
    limit = max_upload_bytes or int(os.environ.get("AI_AGENT_MAX_UPLOAD_BYTES", "10485760"))
    if limit <= 0:
        raise ValueError("The upload limit must be positive")

    # Create storage directories with restricted Unix permissions.
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700) # Allow access only to the owner.

    from backend.local_runtime import heartbeat, readiness

    @asynccontextmanager
    async def lifespan(app):
        async def pulse():
            while True:
                heartbeat(root, "api")
                await asyncio.sleep(2)
        job = asyncio.create_task(pulse())
        try:
            yield
        finally:
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass
            (root / "services" / "api.json").unlink(missing_ok=True)

    app = FastAPI(title="AI Data Agent", version="0.1.0", lifespan=lifespan)
    app.state.dependencies = services

    @app.middleware("http")
    async def prevent_sensitive_response_caching(request, call_next):
        response = await call_next(request)
        if request.url.path == "/api/tasks" or request.url.path.startswith("/api/tasks/") or request.url.path == "/ready":
            response.headers["Cache-Control"] = "no-store"
        return response
    
    # Initialize metadata storage and input file management.
    store = TaskStore(root / "tasks.sqlite3")
    inputs = InputFiles(root / "inputs")
    
    # Register task routes.
    app.include_router(task_router(store, inputs, token, limit))

    @app.get("/health")
    def health():
        """Report API liveness."""
        return {"status": "scaffold", "execution_enabled": False}

    @app.get("/ready")
    def ready(x_demo_token: str | None = Header(None)):
        import hmac
        if x_demo_token is None or not hmac.compare_digest(token, x_demo_token):
            raise HTTPException(401, "Invalid demo token")
        result = readiness(root)
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    # Mount the frontend.
    web = Path(__file__).resolve().parent.parent / "web"
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
    
    return app
