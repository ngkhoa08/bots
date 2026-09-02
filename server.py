from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from browser_bridge import messenger
from cookie_import import register_cookie_import
from live_setup import register_live_setup

app = FastAPI(title="Personal Messenger MCP Bridge", version="0.5.0")
ACCESS_KEY = os.environ.get("MCP_ACCESS_KEY", "").strip()
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26"}


def _check(key: str) -> None:
    if not ACCESS_KEY or key != ACCESS_KEY:
        raise HTTPException(401, "Unauthorized")


def _tool_text(payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(payload, dict):
        out["structuredContent"] = payload
    return out


TOOLS = [
    {
        "name": "messenger_status",
        "title": "Messenger login status",
        "description": "Check whether the Messenger Web session is logged in. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "messenger_list_chats",
        "title": "List Messenger chats",
        "description": "List recent Messenger conversations. Read-only.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "messenger_read_chat",
        "title": "Read Messenger chat",
        "description": "Read recent visible messages from one Messenger conversation. Read-only.",
        "inputSchema": {"type": "object", "properties": {"chat": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30}}, "required": ["chat"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "messenger_send_message",
        "title": "Send Messenger message",
        "description": "Send a Messenger message. Call only when the user explicitly asks to send.",
        "inputSchema": {"type": "object", "properties": {"chat": {"type": "string"}, "message": {"type": "string", "minLength": 1, "maxLength": 4000}}, "required": ["chat", "message"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
]


async def _call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "messenger_status":
            return _tool_text(await messenger.status())
        if name == "messenger_list_chats":
            return _tool_text({"chats": await messenger.list_chats(args.get("limit", 20))})
        if name == "messenger_read_chat":
            return _tool_text(await messenger.read_chat(args["chat"], args.get("limit", 30)))
        if name == "messenger_send_message":
            return _tool_text(await messenger.send_message(args["chat"], args["message"]))
        return _tool_text({"error": f"Unknown tool: {name}"}, True)
    except Exception as exc:
        return _tool_text({"error": str(exc)}, True)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "status": "ok",
        "health": "/health",
        "cookie_login": "/cookie/<key>",
        "live_login": "/live/<key>",
        "mcp": "/mcp/<key>",
    }


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.on_event("shutdown")
async def shutdown() -> None:
    await messenger.close()


@app.get("/mcp/{key}")
async def mcp_get(key: str) -> Response:
    _check(key)
    return Response(status_code=405, headers={"Allow": "POST"})


@app.post("/mcp/{key}")
async def mcp_post(key: str, request: Request) -> Response:
    _check(key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None, "error": {"code": -32600, "message": "Invalid Request"}}, status_code=400)
    if "id" not in body:
        return Response(status_code=202)
    req_id = body.get("id")
    method = body["method"]
    if method == "initialize":
        requested = (body.get("params") or {}).get("protocolVersion", "2025-06-18")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else "2025-06-18"
        result = {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "personal-messenger-render-bridge", "version": "0.5.0"},
            "instructions": "Read the account owner's Messenger Web session. Send messages only when explicitly requested. Never reveal session data or access keys.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = body.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid tool call"}})
        result = await _call_tool(name, args)
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


register_cookie_import(app, ACCESS_KEY)
register_live_setup(app, ACCESS_KEY)
