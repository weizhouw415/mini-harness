from __future__ import annotations

import asyncio
import json
import secrets
from ipaddress import ip_address
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from harness.artifacts.files import MAX_FILE_BYTES, MAX_UPLOADS, file_name
from harness.config import CapabilityConfig, MemoryConfig, ModelConfig, Settings
from harness.core.agent import Agent
from harness.core.llm import ModelError, OpenAICompatible
from harness.execution.router import WorkerCallError, WorkerDisconnected, WorkerManager, WorkerTarget, WorkerUnavailable
from harness.skills.migration import migrate_skill_catalog
from harness.storage.sqlite import Conflict, NotFound, RunStore
from harness.memory.store import MemoryStore
from harness.memory.jobs import MemoryJobs
from harness.skills.jobs import SkillReviewJobs


class RunRequest(BaseModel):
    task: str = Field(default="", max_length=16_000)
    session_id: str | None = None
    worker_id: str | None = None
    worker_target: WorkerTarget | None = None
    approval_mode: Literal["manual", "auto", "allow"] | None = None
    upload_ids: list[Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]] = Field(
        default_factory=list, max_length=MAX_UPLOADS,
    )


class ApprovalRequest(BaseModel):
    call_id: str
    approved: StrictBool


class ApprovalModeRequest(BaseModel):
    approval_mode: Literal["manual", "auto", "allow"]


class CapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: StrictBool
    register_artifacts: StrictBool = Field(alias="register")
    computer_use: StrictBool


class MemorySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    automatic: StrictBool
    personal: StrictBool


class MemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    key: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=4000)
    kind: Literal["preference", "fact", "project", "episode", "short_term"] = "preference"
    scope: Literal["session", "global"] = "session"
    expected_version: int | None = None


class ModelRequest(BaseModel):
    model: str = Field(max_length=256)
    base_url: str = Field(max_length=2048)
    api_key: str | None = Field(default=None, max_length=8192)


class ProviderRequest(ModelRequest):
    name: str = Field(max_length=256)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "developer"]
    content: str


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    stream: bool = False
    session_id: str | None = None
    worker_id: str | None = None


def create_app(settings: Settings | None = None, *, llm=None, worker_manager=None,
               local_ui=False) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.api_token:
        raise ValueError("Set HARNESS_API_TOKEN to protect the API.")
    if not settings.worker_secret and worker_manager is None:
        raise ValueError("Set HARNESS_WORKER_SECRET or HARNESS_WORKER_SECRETS to authenticate workers.")
    if settings.max_steps < 1 or settings.context_chars < 1_000 or settings.max_tool_seconds < 1:
        raise ValueError("Invalid run limits.")
    store = RunStore(settings.data_dir)
    model_config = ModelConfig(settings)
    capability_config = CapabilityConfig(settings)
    memory_config = MemoryConfig(settings)
    memory_store = MemoryStore(store)

    def retain_run(worker_id: str, run_id: str, workspace_id: str) -> bool:
        try:
            run = store.get(run_id)
        except NotFound:
            return False
        return (
            run["worker_id"] == worker_id and run["workspace_id"] == workspace_id
            and run["status"] in {"queued", "running", "awaiting_approval", "paused"}
        )

    workers = (worker_manager if worker_manager is not None
               else WorkerManager(settings.worker_secret, retain_run=retain_run))
    migrate_skill_catalog(settings.data_dir / "skills", settings.skills_dir)
    llm = llm if llm is not None else OpenAICompatible(model_config)
    agent = Agent(settings, store, workers, llm, capability_config=capability_config,
                  memory_store=memory_store, memory_config=memory_config)
    memory_jobs = MemoryJobs(store, memory_store, llm, memory_config)
    skill_jobs = SkillReviewJobs(store, llm, settings.skills_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.recover_interrupted()
        memory_jobs.start()
        skill_jobs.start()
        try:
            yield
        finally:
            await skill_jobs.stop()
            await memory_jobs.stop()
            await agent.stop()

    app = FastAPI(title="Mini Harness", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.workers = store, workers

    async def authorize(authorization: str = Header(default="")):
        prefix, _, value = authorization.partition(" ")
        if prefix.lower() != "bearer" or not secrets.compare_digest(
            value.encode("utf-8"), settings.api_token.encode("utf-8")
        ):
            raise HTTPException(401, "Bearer authentication required.", headers={"WWW-Authenticate": "Bearer"})

    protected = [Depends(authorize)]

    def is_local_ui(request: Request) -> bool:
        if (
            not local_ui or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or request.headers.get("sec-fetch-site") != "same-origin"
            or request.headers.get("x-harness-ui") != "1"
            or any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host"))
            or request.client is None
        ):
            return False
        try:
            return ip_address(request.client.host).is_loopback
        except ValueError:
            return False

    async def authorize_control(request: Request, authorization: str = Header(default="")):
        if not is_local_ui(request):
            await authorize(authorization)

    control = [Depends(authorize_control)]

    @app.exception_handler(NotFound)
    async def not_found(_, error):
        return JSONResponse({"detail": str(error)}, 404)

    @app.exception_handler(Conflict)
    async def conflict(_, error):
        return JSONResponse({"detail": str(error)}, 409)

    @app.exception_handler(ModelError)
    async def model_error(_, error):
        return JSONResponse({"detail": str(error)}, 502)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    web = Path(__file__).with_name("web")
    # Keep the UI paired with the routes loaded by this process. Serving files
    # from disk on each request can mix a newly updated UI with an older server.
    web_media_types = {
        "index.html": "text/html",
        "app.js": "text/javascript",
        "style.css": "text/css",
        "settings.css": "text/css",
        "markdown.js": "text/javascript",
        "markdown.css": "text/css",
        "marked.umd.js": "text/javascript",
        "purify.min.js": "text/javascript",
    }
    web_assets = {name: (web / name).read_bytes() for name in web_media_types}

    @app.get("/", include_in_schema=False)
    async def web_ui():
        return Response(web_assets["index.html"], media_type=web_media_types["index.html"], headers={
            "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; "
                                       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        })

    def model_status():
        config = model_config.values
        return JSONResponse({
            "configured": bool(config["model"]), "model": config["model"],
            "base_url": config["base_url"], "has_api_key": bool(config["api_key"]),
        }, headers={"Cache-Control": "no-store"})

    def providers_status():
        return JSONResponse({
            "providers": [{"id": p["id"], "name": p["name"], "model": p["model"],
                           "base_url": p["base_url"], "has_api_key": bool(p["api_key"])}
                          for p in model_config.providers],
            "active_provider_id": model_config.active_provider_id,
            "configured": bool(model_config.values["model"]),
        }, headers={"Cache-Control": "no-store"})

    def require_local_model_access(request: Request):
        if not is_local_ui(request):
            raise HTTPException(403, "Open the Web UI on the local server's loopback address.")

    @app.get("/ui/memory/settings", include_in_schema=False)
    async def read_memory_settings(request: Request):
        require_local_model_access(request)
        return JSONResponse(memory_config.values, headers={"Cache-Control": "no-store"})

    @app.put("/ui/memory/settings", include_in_schema=False)
    async def write_memory_settings(request: Request, config: MemorySettingsRequest):
        require_local_model_access(request)
        memory_config.save(config.model_dump())
        return JSONResponse(memory_config.values, headers={"Cache-Control": "no-store"})

    @app.get("/v1/memory", dependencies=control)
    async def search_memory(session_id: str, q: str = ""):
        return memory_store.search(session_id, q, limit=50, include_global=memory_config.values["personal"])

    @app.post("/v1/memory", dependencies=control)
    async def remember(request: MemoryRequest):
        if request.scope == "global" and not memory_config.values["personal"]:
            raise HTTPException(403, "Personal memory is disabled.")
        try:
            return memory_store.remember(request.session_id, request.key, request.content, scope=request.scope,
                                         kind=request.kind, expected_version=request.expected_version)
        except ValueError as error:
            if isinstance(error, (Conflict, NotFound)):
                raise
            raise HTTPException(422, str(error)) from error

    @app.get("/v1/memory/profile", dependencies=control)
    async def memory_profile(session_id: str):
        items = memory_store.search(session_id, limit=100, include_global=memory_config.values["personal"])
        return [item for item in items if item["kind"] == "preference"][:10]

    @app.get("/v1/memory/{memory_id}", dependencies=control)
    async def get_memory(memory_id: str, session_id: str):
        return memory_store.get(session_id, memory_id, include_global=memory_config.values["personal"])

    @app.get("/v1/memory/{memory_id}/source", dependencies=control)
    async def memory_source(memory_id: str, session_id: str):
        return memory_store.source(session_id, memory_id, include_global=memory_config.values["personal"])

    @app.delete("/v1/memory/{memory_id}", dependencies=control)
    async def forget_memory(memory_id: str, session_id: str):
        return memory_store.forget(session_id, memory_id, include_global=memory_config.values["personal"])

    @app.get("/ui/capabilities", include_in_schema=False)
    async def read_capabilities(request: Request):
        require_local_model_access(request)
        return JSONResponse(capability_config.values, headers={"Cache-Control": "no-store"})

    @app.put("/ui/capabilities", include_in_schema=False)
    async def write_capabilities(request: Request, config: CapabilityRequest):
        require_local_model_access(request)
        capability_config.save(config.model_dump(by_alias=True))
        return JSONResponse(capability_config.values, headers={"Cache-Control": "no-store"})

    def validate_model(config: ModelRequest):
        model, base_url = config.model.strip(), config.base_url.strip().rstrip("/")
        try:
            url = httpx.URL(base_url)
            valid_url = (url.scheme in {"http", "https"} and url.host and not url.userinfo
                         and not url.query and not url.fragment and not any(c in base_url for c in "?#"))
        except (httpx.InvalidURL, ValueError):
            valid_url = False
        if not model or not valid_url:
            raise HTTPException(422, "Enter a model name and a valid HTTP(S) Base URL without credentials or query.")
        if config.api_key is not None and any(not 33 <= ord(c) <= 126 for c in config.api_key):
            raise HTTPException(422, "The API key must contain only printable ASCII characters without spaces.")
        return model, base_url

    def validate_provider(config: ProviderRequest):
        name = config.name.strip()
        if not name:
            raise HTTPException(422, "Enter a provider name.")
        model, base_url = validate_model(config)
        return name, model, base_url

    def require_provider(provider_id: str):
        if model_config.provider(provider_id) is None:
            raise HTTPException(404, "Provider not found.")

    @app.get("/ui/model", include_in_schema=False)
    async def read_model(request: Request):
        require_local_model_access(request)
        return model_status()

    @app.put("/ui/model", include_in_schema=False)
    async def write_model(request: Request, config: ModelRequest):
        require_local_model_access(request)
        model, base_url = validate_model(config)
        model_config.save(model, base_url, config.api_key)
        return model_status()

    @app.get("/ui/providers", include_in_schema=False)
    async def read_providers(request: Request):
        require_local_model_access(request)
        return providers_status()

    @app.post("/ui/providers", status_code=201, include_in_schema=False)
    async def create_provider(request: Request, config: ProviderRequest):
        require_local_model_access(request)
        name, model, base_url = validate_provider(config)
        model_config.create(name, model, base_url, config.api_key)
        response = providers_status()
        response.status_code = 201
        return response

    @app.put("/ui/providers/{provider_id}", include_in_schema=False)
    async def update_provider(request: Request, provider_id: str, config: ProviderRequest):
        require_local_model_access(request)
        require_provider(provider_id)
        name, model, base_url = validate_provider(config)
        model_config.update(provider_id, name, model, base_url, config.api_key)
        return providers_status()

    @app.put("/ui/providers/{provider_id}/activate", include_in_schema=False)
    async def activate_provider(request: Request, provider_id: str):
        require_local_model_access(request)
        require_provider(provider_id)
        model_config.activate(provider_id)
        return providers_status()

    @app.delete("/ui/providers/{provider_id}", include_in_schema=False)
    async def delete_provider(request: Request, provider_id: str):
        require_local_model_access(request)
        require_provider(provider_id)
        model_config.delete(provider_id)
        return providers_status()

    @app.get("/ui/{asset}", include_in_schema=False)
    async def web_asset(asset: str):
        if asset == "index.html" or asset not in web_assets:
            raise HTTPException(404, "Asset not found.")
        return Response(web_assets[asset], media_type=web_media_types[asset], headers={"Cache-Control": "no-cache"})

    @app.websocket("/v1/workers/ws")
    async def worker_socket(websocket: WebSocket):
        await workers.accept(websocket)

    @app.get("/v1/workers", dependencies=control)
    async def worker_list():
        return workers.list_workers()

    @app.post("/v1/uploads", status_code=201, dependencies=control)
    async def upload_file(request: Request, x_file_name: str = Header()):
        try:
            name = file_name(unquote(x_file_name, encoding="utf-8", errors="strict"))
            if name.lower().startswith(".upload-"):
                raise ValueError("This file name is reserved for uploads.")
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPException(415, "Send the raw file as application/octet-stream.")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_FILE_BYTES:
                raise HTTPException(413, "Each file may be up to 20 MiB.")
            content.extend(chunk)
        return await asyncio.to_thread(store.add_upload, name, bytes(content))

    def start(task: str, session_id: str | None, worker_id: str | None,
              approval_mode: Literal["manual", "auto", "allow", "legacy"] = "legacy",
              upload_ids: list[str] | None = None, *, worker_target: WorkerTarget | None = None,
              memory_input: str | None = None):
        if upload_ids and not task.strip():
            task = "请查看我上传的附件。"
        if not task.strip() or len(task) > 16_000:
            raise HTTPException(422, "The task must contain 1 to 16000 characters.")
        if worker_id is not None and worker_target is not None:
            raise HTTPException(422, "Specify either worker_id or worker_target.")
        run = store.create(task, session_id, worker_id, approval_mode, upload_ids=upload_ids,
                           worker_target=worker_target, memory_input=memory_input if memory_input is not None else task)
        agent.schedule(run["id"])
        return store.get(run["id"], include_files=True)

    @app.post("/v1/runs", status_code=202, dependencies=control)
    async def start_run(request: RunRequest):
        return start(request.task, request.session_id, request.worker_id, request.approval_mode or "legacy",
                     request.upload_ids, worker_target=request.worker_target)

    @app.get("/v1/runs", dependencies=control)
    async def list_runs():
        return store.recent()

    @app.get("/v1/sessions", dependencies=control)
    async def list_sessions():
        return store.recent_sessions()

    @app.get("/v1/sessions/{session_id}/runs", dependencies=control)
    async def list_session_runs(session_id: str):
        return store.session_runs(session_id)

    @app.get("/v1/runs/{run_id}", dependencies=control)
    async def get_run(run_id: str):
        return store.get(run_id, include_files=True)

    @app.get("/v1/runs/{run_id}/skill-review", dependencies=control)
    async def get_skill_review(run_id: str):
        store.get(run_id)
        with store.connect() as db:
            row = db.execute("SELECT status, result FROM skill_review_jobs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return {"status": "not_scheduled"}
        if row["status"] == "done" and row["result"]:
            return json.loads(row["result"])
        return {"status": row["status"]}

    @app.get("/v1/runs/{run_id}/files", dependencies=control)
    async def run_files(run_id: str):
        return store.files(run_id)

    @app.get("/v1/runs/{run_id}/files/{file_id}", dependencies=control)
    async def download_file(run_id: str, file_id: str):
        file, path = store.run_file(run_id, file_id)
        return FileResponse(path, filename=file["name"], media_type="application/octet-stream", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    @app.get("/v1/runs/{run_id}/events", dependencies=control)
    async def get_events(run_id: str, after: int = 0):
        return store.events(run_id, after=after)

    @app.get("/v1/runs/{run_id}/artifacts/{name}", dependencies=control)
    async def artifact(run_id: str, name: str):
        store.get(run_id)
        path = store.image_path(run_id, name)
        if not path.is_file():
            raise NotFound("Artifact not found.")
        return FileResponse(path, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/v1/runs/{run_id}/approve", status_code=202, dependencies=control)
    async def decide(run_id: str, request: ApprovalRequest):
        run = store.get(run_id)
        if run["status"] != "awaiting_approval" or run["approval"]["call_id"] != request.call_id:
            raise Conflict("This call is not awaiting approval.")
        store.transition(run_id, {"awaiting_approval"}, "queued",
                         event=("approval_decision", request.model_dump()))
        agent.schedule(run_id)
        return store.get(run_id)

    @app.patch("/v1/runs/{run_id}/approval-mode", dependencies=control)
    async def change_approval_mode(run_id: str, request: ApprovalModeRequest):
        run, requeued = store.update_approval_mode(run_id, request.approval_mode)
        if requeued:
            agent.schedule(run_id)
        return run

    @app.post("/v1/runs/{run_id}/resume", status_code=202, dependencies=control)
    async def resume_run(run_id: str):
        store.get(run_id)
        store.transition(run_id, {"paused"}, "queued")
        agent.schedule(run_id)
        return store.get(run_id)

    @app.post("/v1/runs/{run_id}/cancel", dependencies=control)
    async def cancel_run(run_id: str):
        run = store.get(run_id)
        store.transition(run_id, {"queued", "running", "paused", "awaiting_approval"}, "cancelled")
        task = agent.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if run["worker_id"]:
            try:
                await workers.release(run["worker_id"], run_id)
            except (WorkerUnavailable, WorkerDisconnected, WorkerCallError):
                pass
        return store.get(run_id)

    @app.post("/v1/chat/completions", dependencies=protected)
    async def chat(request: ChatRequest):
        if request.stream:
            raise HTTPException(400, "Streaming is unavailable. Use /v1/runs to poll events.")
        model = model_config.values["model"]
        if request.model and model and request.model != model:
            raise HTTPException(404, "Model does not match the configured model.")
        if request.messages[-1].role != "user":
            raise HTTPException(422, "The final message must have the user role.")
        if len(request.messages) == 1:
            task = request.messages[0].content
        else:
            task = "\n\n".join(f"{message.role}: {message.content}" for message in request.messages)
        run = start(task, request.session_id, request.worker_id, memory_input=request.messages[-1].content)
        deadline = asyncio.get_running_loop().time() + 60
        while run["status"] in {"queued", "running"} and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.15)
            run = store.get(run["id"])
        if run["status"] != "completed":
            raise HTTPException(409, {"run_id": run["id"], "status": run["status"], "error": run["error"]})
        return {
            "id": f"chatcmpl-{run['id']}", "object": "chat.completion",
            "created": int(datetime.fromisoformat(run["created_at"]).timestamp()),
            "model": model or request.model or "mini-harness",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": run["output"]},
                         "finish_reason": "stop"}],
            "session_id": run["session_id"], "run_id": run["id"],
        }

    return app
