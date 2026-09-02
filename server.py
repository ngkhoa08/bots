from __future__ import annotations

import base64
import hmac
import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from browser_bridge import MESSENGER_URL, RUNTIME_STATE, messenger

app = FastAPI(title="Personal Messenger MCP Bridge - Render", version="0.3.0")

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
    host = request.headers.get("host", "")
    same_origin = bool(origin and origin.rstrip("/") == f"{request.url.scheme}://{host}".rstrip("/"))
    if origin and origin not in ALLOWED_ORIGINS and not same_origin:
        return JSONResponse({"detail": f"Origin not allowed: {origin}"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/setup/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


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
        "setup": "/setup/<MCP_ACCESS_KEY>",
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


SETUP_HTML = r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Messenger Login - Render</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#eee;margin:0;padding:16px}main{max-width:1060px;margin:auto}.card{background:#1d1d1d;border:1px solid #333;border-radius:14px;padding:14px;margin-bottom:12px}h2{margin:0 0 8px}.warn{color:#ffca76}.ok{color:#78df9a}#screen{width:100%;height:auto;display:block;background:#fff;border-radius:10px;cursor:crosshair;touch-action:manipulation}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}input{flex:1;min-width:220px;padding:12px;border-radius:9px;border:1px solid #555;background:#111;color:#fff;font-size:16px}button{padding:11px 14px;border:0;border-radius:9px;cursor:pointer;font-weight:600}#msg{white-space:pre-wrap;font-size:14px}small{color:#aaa}code{word-break:break-all}</style></head>
<body><main><div class="card"><h2>Đăng nhập Messenger trên browser của Render</h2>
<div class="warn">Đây là bộ điều khiển Chromium trên chính Render của bạn, không phải form đăng nhập Facebook tự tạo.</div>
<small>Click vào ảnh để click trong browser từ xa. Sau đó nhập chữ vào ô bên dưới và bấm “Gõ”. Mật khẩu đi qua service Render của chính bạn và ứng dụng không ghi request body vào log.</small>
<div id="msg">Đang khởi động...</div></div>
<div class="card"><img id="screen" alt="Remote browser screenshot"></div>
<div class="card"><div class="row"><input id="txt" autocomplete="off" placeholder="Nội dung cần gõ vào ô đang chọn"><button onclick="typeText()">Gõ</button><button onclick="keyPress('Tab')">Tab</button><button onclick="keyPress('Enter')">Enter</button><button onclick="keyPress('Backspace')">⌫</button></div>
<div class="row"><button onclick="saveSession()">✅ Lưu phiên đăng nhập</button><button onclick="restartLogin()">Mở lại Messenger</button><button onclick="clearSession()">Xóa phiên & đăng nhập lại</button></div></div>
</main><script>
const base=location.pathname.replace(/\/$/,''); const img=document.getElementById('screen'), msg=document.getElementById('msg'), txt=document.getElementById('txt');
async function post(s,data={}){const r=await fetch(base+s,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});const j=await r.json().catch(()=>({error:'HTTP '+r.status}));if(!r.ok)throw new Error(j.detail||j.error||('HTTP '+r.status));return j}
async function snap(){try{const j=await post('/snapshot');img.src=j.image;msg.innerHTML=(j.logged_in?'<span class="ok">● Đã đăng nhập</span>':'<span class="warn">● Chưa xác nhận đăng nhập</span>')+' — '+j.url}catch(e){msg.textContent=e.message}}
async function start(){try{await post('/start');await snap()}catch(e){msg.textContent=e.message}}
img.addEventListener('click',async e=>{const r=img.getBoundingClientRect();const x=(e.clientX-r.left)*1024/r.width;const y=(e.clientY-r.top)*720/r.height;try{await post('/click',{x,y});setTimeout(snap,350)}catch(er){msg.textContent=er.message}});
async function typeText(){if(!txt.value)return;const v=txt.value;txt.value='';try{await post('/type',{text:v});setTimeout(snap,350)}catch(e){msg.textContent=e.message}}
async function keyPress(key){try{await post('/key',{key});setTimeout(snap,300)}catch(e){msg.textContent=e.message}}
async function saveSession(){try{const j=await post('/save');msg.innerHTML='<span class="ok">Đã lưu phiên. '+(j.logged_in?'Messenger đang đăng nhập.':'')+'</span>';await snap()}catch(e){msg.textContent=e.message}}
async function restartLogin(){try{await post('/start');await snap()}catch(e){msg.textContent=e.message}}
async function clearSession(){if(!confirm('Xóa session hiện tại và đăng nhập lại?'))return;try{await post('/clear');await start()}catch(e){msg.textContent=e.message}}
txt.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();typeText()}}); start(); setInterval(snap,1800);
</script></body></html>'''


class SetupClick(BaseModel):
    x: float
    y: float


class SetupType(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class SetupKey(BaseModel):
    key: str


async def _setup_page() -> Any:
    page = await messenger._get_page_unlocked()
    if page.url == "about:blank":
        await page.goto(MESSENGER_URL, wait_until="domcontentloaded", timeout=35_000)
        await page.wait_for_timeout(900)
    messenger._touch_unlocked()
    return page


@app.get("/setup/{key}", response_class=HTMLResponse)
async def setup_ui(request: Request, key: str) -> HTMLResponse:
    _require_auth(request, key)
    return HTMLResponse(SETUP_HTML)


@app.post("/setup/{key}/start")
async def setup_start(request: Request, key: str) -> Any:
    _require_auth(request, key)
    async with messenger._lock:
        page = await messenger._get_page_unlocked()
        await page.goto(MESSENGER_URL, wait_until="domcontentloaded", timeout=35_000)
        await page.wait_for_timeout(900)
        messenger._touch_unlocked()
        return {"ok": True, "url": page.url}


@app.post("/setup/{key}/snapshot")
async def setup_snapshot(request: Request, key: str) -> Any:
    _require_auth(request, key)
    async with messenger._lock:
        page = await _setup_page()
        shot = await page.screenshot(type="jpeg", quality=55)
        logged_in = await messenger._is_logged_in(page)
        return {
            "image": "data:image/jpeg;base64," + base64.b64encode(shot).decode("ascii"),
            "url": page.url,
            "logged_in": logged_in,
        }


@app.post("/setup/{key}/click")
async def setup_click(request: Request, key: str, body: SetupClick) -> Any:
    _require_auth(request, key)
    x = max(0.0, min(float(body.x), 1024.0))
    y = max(0.0, min(float(body.y), 720.0))
    async with messenger._lock:
        page = await _setup_page()
        await page.mouse.click(x, y)
        await page.wait_for_timeout(250)
        messenger._touch_unlocked()
        return {"ok": True, "url": page.url}


@app.post("/setup/{key}/type")
async def setup_type(request: Request, key: str, body: SetupType) -> Any:
    _require_auth(request, key)
    async with messenger._lock:
        page = await _setup_page()
        await page.keyboard.type(body.text, delay=18)
        await page.wait_for_timeout(150)
        messenger._touch_unlocked()
        return {"ok": True}


@app.post("/setup/{key}/key")
async def setup_key(request: Request, key: str, body: SetupKey) -> Any:
    _require_auth(request, key)
    allowed = {"Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space"}
    if body.key not in allowed:
        raise HTTPException(400, "Unsupported key")
    async with messenger._lock:
        page = await _setup_page()
        await page.keyboard.press(body.key)
        await page.wait_for_timeout(150)
        messenger._touch_unlocked()
        return {"ok": True}


@app.post("/setup/{key}/save")
async def setup_save(request: Request, key: str) -> Any:
    _require_auth(request, key)
    async with messenger._lock:
        page = await _setup_page()
        logged_in = await messenger._is_logged_in(page)
        if not logged_in:
            raise HTTPException(400, "Messenger chưa xác nhận đăng nhập. Hoàn tất đăng nhập trước rồi bấm Lưu phiên.")
        await messenger._persist_runtime_state_unlocked()
        messenger._touch_unlocked()
        return {"saved": True, "logged_in": True, "runtime_state": str(RUNTIME_STATE)}


@app.post("/setup/{key}/clear")
async def setup_clear(request: Request, key: str) -> Any:
    _require_auth(request, key)
    async with messenger._lock:
        await messenger._close_unlocked()
        try:
            RUNTIME_STATE.unlink(missing_ok=True)
        except Exception:
            pass
        return {"cleared": True}


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
                    "version": "0.3.0",
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
