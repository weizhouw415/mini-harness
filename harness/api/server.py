from __future__ import annotations

import asyncio
import importlib
import os
import secrets
from ipaddress import ip_address
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, StrictBool

from harness.config import ModelConfig, Settings
from harness.core.agent import Agent
from harness.core.llm import ModelError, OpenAICompatible
from harness.execution.router import WorkerCallError, WorkerDisconnected, WorkerManager, WorkerUnavailable
from harness.skills import SkillError, SkillStore, evaluate_skill, learn_skill
from harness.storage.sqlite import Conflict, NotFound, RunStore


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=16_000)
    session_id: str | None = None
    worker_id: str | None = None
    approval_mode: Literal["manual", "auto", "allow"] | None = None


class ApprovalRequest(BaseModel):
    call_id: str
    approved: StrictBool


class ApprovalModeRequest(BaseModel):
    approval_mode: Literal["manual", "auto", "allow"]


class ModelRequest(BaseModel):
    model: str = Field(max_length=256)
    base_url: str = Field(max_length=2048)
    api_key: str | None = Field(default=None, max_length=8192)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "developer"]
    content: str


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    stream: bool = False
    session_id: str | None = None
    worker_id: str | None = None


class EvaluateRequest(BaseModel):
    environment: Literal["cloud-browser", "macos-desktop", "windows-desktop"]


class PublishRequest(BaseModel):
    approved: StrictBool


def configured_evaluator():
    target = os.environ.get("HARNESS_SKILL_EVALUATOR", "")
    if not target:
        return None
    module, separator, function = target.partition(":")
    if not separator:
        raise ValueError("HARNESS_SKILL_EVALUATOR must be in module:function format")
    return getattr(importlib.import_module(module), function)


def create_app(settings: Settings | None = None, *, llm=None, worker_manager=None,
               skill_evaluator=None, local_ui=False) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.api_token:
        raise ValueError("Set HARNESS_API_TOKEN to protect the API.")
    if not settings.worker_secret and worker_manager is None:
        raise ValueError("Set HARNESS_WORKER_SECRET or HARNESS_WORKER_SECRETS to authenticate workers.")
    if settings.max_steps < 1 or settings.context_chars < 1_000 or settings.max_tool_seconds < 1:
        raise ValueError("Invalid run limits.")
    store = RunStore(settings.data_dir)
    model_config = ModelConfig(settings)

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
    skills = SkillStore(settings.data_dir / "skills")
    llm = llm if llm is not None else OpenAICompatible(model_config)
    agent = Agent(settings, store, workers, skills, llm)
    evaluator = skill_evaluator if skill_evaluator is not None else configured_evaluator()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.recover_interrupted()
        try:
            yield
        finally:
            await agent.stop()

    app = FastAPI(title="Mini Harness", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.workers, app.state.skills = store, workers, skills

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

    @app.exception_handler(SkillError)
    async def invalid_skill(_, error):
        return JSONResponse({"detail": str(error)}, 422)

    @app.exception_handler(ModelError)
    async def model_error(_, error):
        return JSONResponse({"detail": str(error)}, 502)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    web = Path(__file__).with_name("web")

    @app.get("/", include_in_schema=False)
    async def web_ui():
        return FileResponse(web / "index.html", headers={
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

    @app.get("/ui/model", include_in_schema=False)
    async def read_model(request: Request):
        if not is_local_ui(request):
            raise HTTPException(403, "Open the Web UI on the local server's loopback address.")
        return model_status()

    @app.put("/ui/model", include_in_schema=False)
    async def write_model(request: Request, config: ModelRequest):
        if not is_local_ui(request):
            raise HTTPException(403, "Open the Web UI on the local server's loopback address.")
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
        model_config.save(model, base_url, config.api_key)
        return model_status()

    @app.get("/ui/{asset}", include_in_schema=False)
    async def web_asset(asset: Literal["app.js", "style.css", "markdown.js", "markdown.css",
                                      "marked.umd.js", "purify.min.js"]):
        return FileResponse(web / asset, headers={"Cache-Control": "no-cache"})

    @app.websocket("/v1/workers/ws")
    async def worker_socket(websocket: WebSocket):
        await workers.accept(websocket)

    @app.get("/v1/workers", dependencies=control)
    async def worker_list():
        return workers.list_workers()

    def start(task: str, session_id: str | None, worker_id: str | None,
              approval_mode: Literal["manual", "auto", "allow", "legacy"] = "legacy"):
        if not task.strip() or len(task) > 16_000:
            raise HTTPException(422, "The task must contain 1 to 16000 characters.")
        run = store.create(task, session_id, worker_id, approval_mode)
        agent.schedule(run["id"])
        return run

    @app.post("/v1/runs", status_code=202, dependencies=control)
    async def start_run(request: RunRequest):
        return start(request.task, request.session_id, request.worker_id, request.approval_mode or "legacy")

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
        return store.get(run_id)

    @app.get("/v1/runs/{run_id}/events", dependencies=control)
    async def get_events(run_id: str, after: int = 0):
        return [event for event in store.events(run_id) if event["seq"] > after]

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
        run = start(task, request.session_id, request.worker_id)
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

    @app.get("/v1/skills", dependencies=protected)
    async def skill_index(environment: str | None = None):
        return skills.index(environment)

    @app.post("/v1/runs/{run_id}/learn", dependencies=protected)
    async def learn(run_id: str):
        run = store.get(run_id)
        if run["status"] != "completed":
            raise Conflict("Learning requires a completed run.")

        async def generate(messages: list[dict]):
            reply = await llm.complete(messages, [])
            return reply.get("content")

        return await learn_skill(skills, {**run, "events": store.events(run_id)}, generate)

    @app.post("/v1/skills/{name}/{version}/evaluate", dependencies=protected)
    async def evaluate(name: str, version: str, request: EvaluateRequest):
        if evaluator is None:
            raise HTTPException(503, "Register a trusted, isolated evaluator with HARNESS_SKILL_EVALUATOR.")
        return await evaluate_skill(skills, name, version, request.environment, evaluator)

    @app.post("/v1/skills/{name}/{version}/publish", dependencies=protected)
    async def publish(name: str, version: str, request: PublishRequest):
        return skills.publish(name, version, approved=request.approved)

    @app.post("/v1/skills/{name}/{version}/rollback", dependencies=protected)
    async def rollback(name: str, version: str, request: PublishRequest):
        return skills.rollback(name, version, approved=request.approved)

    return app
