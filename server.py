from __future__ import annotations

import hmac
import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from browser_bridge import messenger

app = FastAPI(title="Personal Messenger MCP Bridge - Render", version="0.2.0")

ALLOWED_ORIGINS = {
    x.strip() for x in os.getenv(
        "ALLOWED_ORIGINS",
        "https://chatgpt.com,https://chat.openai.com,http://localhost,http://127.0.0.1",
    ).split(",") if x.strip()
}
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26"}
ACCESS_KEY = os.getenv("MCP_ACCESS_KEY", "").strip()


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": f"Origin not allowed: {origin}"}, status_code=403)
    return await call_next(request)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await messenger.close()


def _authorized(request: Request, key: str | None = None) -> bool:
    if not ACCESS_KEY:
        return False
    if key and hmac.compare_digest(key, ACCESS_KEY):
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return hmac.compare_digest(token, ACCESS_KEY)
    return False


def _require_auth(request: Request, key: str | None = None) -> None:
    if not _authorized(request, key):
        raise HTTPException(401, "Unauthorized")


def tool_text(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if isinstance(payload, dict):
        out["structuredContent"] = payload
    return out


TOOLS = [
    {
        "name": "messenger_status",
        "title": "Messenger login status",
        "description": "Check whether the restored Messenger Web session is logged in. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "messenger_list_chats",
        "title": "List Messenger chats",
        "description": "List recent Messenger conversations from the account owner's Messenger Web session. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "messenger_read_chat",
        "title": "Read Messenger chat",
        "description": "Read visible recent messages from one Messenger conversation. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat": {"type": "string", "description": "Unique chat name or messenger.com/t/... URL"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            },
            "required": ["chat"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "messenger_send_message",
        "title": "Send Messenger message",
        "description": "Send a message from the restored Messenger account. Use only when the user explicitly asks to send.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat": {"type": "string", "description": "Unique chat name or messenger.com/t/... URL"},
                "message": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "required": ["chat", "message"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
]


async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "messenger_status":
            return tool_text(await messenger.status())
        if name == "messenger_list_chats":
            return tool_text({"chats": await messenger.list_chats(args.get("limit", 20))})
        if name == "messenger_read_chat":
            return tool_text(await messenger.read_chat(args["chat"], args.get("limit", 30)))
        if name == "messenger_send_message":
            return tool_text(await messenger.send_message(args["chat"], args["message"]))
        raise KeyError(name)
    except KeyError:
        raise
    except Exception as exc:
        return tool_text({"error": str(exc)}, is_error=True)


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "Personal Messenger MCP Bridge - Render",
        "health": "/health",
        "mcp": "/mcp/<MCP_ACCESS_KEY>",
        "browser": "lazy-start + idle auto-close",
        "warning": "Unofficial Messenger Web automation. Never expose your session secret or access key.",
    }


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/status")
async def api_status(request: Request) -> Any:
    _require_auth(request)
    return await messenger.status()


class SendBody(BaseModel):
    chat: str
    message: str = Field(min_length=1, max_length=4000)


@app.get("/api/chats")
async def api_chats(request: Request, limit: int = 20) -> Any:
    _require_auth(request)
    return {"chats": await messenger.list_chats(limit)}


@app.get("/api/messages")
async def api_messages(request: Request, chat: str, limit: int = 30) -> Any:
    _require_auth(request)
    return await messenger.read_chat(chat, limit)


@app.post("/api/send")
async def api_send(request: Request, body: SendBody) -> Any:
    _require_auth(request)
    return await messenger.send_message(body.chat, body.message)


async def _handle_mcp(request: Request, key: str | None = None) -> Response:
    _require_auth(request, key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": body.get("id") if isinstance(body, dict) else None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }, status_code=400)

    method = body["method"]
    req_id = body.get("id")
    if "id" not in body:
        return Response(status_code=202)

    try:
        if method == "initialize":
            client_version = (body.get("params") or {}).get("protocolVersion", "2025-06-18")
            protocol = client_version if client_version in SUPPORTED_PROTOCOLS else "2025-06-18"
            result = {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "personal-messenger-render-bridge",
                    "title": "Personal Messenger Render Bridge",
                    "version": "0.2.0",
                },
                "instructions": (
                    "This server reads the account owner's Messenger Web session. "
                    "Call messenger_send_message only after the user explicitly asks to send. "
                    "Never reveal cookies, session state, or access keys."
                ),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = body.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(name, str):
                raise ValueError("Missing tool name")
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            try:
                result = await call_tool(name, args)
            except KeyError:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"},
                })
        else:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })
    except Exception as exc:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": str(exc)},
        })

    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


@app.get("/mcp")
@app.get("/mcp/{key}")
async def mcp_get(request: Request, key: str | None = None) -> Response:
    _require_auth(request, key)
    return Response(status_code=405, headers={"Allow": "POST"})


@app.post("/mcp")
async def mcp_post(request: Request) -> Response:
    return await _handle_mcp(request)


@app.post("/mcp/{key}")
async def mcp_post_key(request: Request, key: str) -> Response:
    return await _handle_mcp(request, key)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "10000")), reload=False)
