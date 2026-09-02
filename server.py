from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from browser_bridge import MESSENGER_URL, messenger
from cookie_import import register_cookie_import
from live_setup import register_live_setup

app = FastAPI(title="Personal Messenger MCP Bridge", version="0.7.0")
ACCESS_KEY = os.environ.get("MCP_ACCESS_KEY", "").strip()
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26"}


async def _try_message_pin(page) -> dict[str, Any]:
    """Submit the Messenger secure-storage PIN only on an obvious restore prompt."""
    pin = os.environ.get("FACEBOOK_MESSAGE_PIN", "").strip()
    if not pin or "facebook.com" not in page.url.lower():
        return {"attempted": False, "reason": "not_configured_or_not_facebook"}

    try:
        body = (await page.locator("body").inner_text(timeout=3500)).lower()
    except Exception:
        return {"attempted": False, "reason": "body_unavailable"}

    prompt_markers = (
        "enter your pin",
        "enter pin",
        "nhập mã pin",
        "nhập pin",
        "message history",
        "secure storage",
        "restore your chats",
        "restore chat history",
        "khôi phục lịch sử",
        "khôi phục đoạn chat",
        "mã pin để khôi phục",
    )
    if not any(marker in body for marker in prompt_markers):
        return {"attempted": False, "reason": "no_pin_prompt"}
    if "log into facebook" in body or "đăng nhập facebook" in body:
        return {"attempted": False, "reason": "login_form_guard"}

    selectors = [
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[aria-label*="PIN" i]',
        'input[placeholder*="PIN" i]',
        'input[type="password"]',
    ]

    target = None
    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = min(await loc.count(), 8)
        except Exception:
            continue
        for i in range(count):
            candidate = loc.nth(i)
            try:
                if await candidate.is_visible():
                    target = candidate
                    break
            except Exception:
                continue
        if target is not None:
            break

    if target is None:
        return {"attempted": False, "reason": "pin_input_not_found"}

    try:
        await target.fill(pin)
    except Exception:
        try:
            await target.click()
            await page.keyboard.type(pin, delay=55)
        except Exception:
            return {"attempted": True, "submitted": False, "reason": "fill_failed"}

    clicked = False
    for label in (
        "Continue", "Tiếp tục", "Confirm", "Xác nhận", "Restore", "Khôi phục",
        "Submit", "Gửi", "Done", "Xong",
    ):
        try:
            button = page.get_by_role("button", name=label, exact=False)
            if await button.count() and await button.first.is_visible():
                await button.first.click()
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        try:
            await target.press("Enter")
        except Exception:
            pass

    await page.wait_for_timeout(2200)
    messenger._touch_unlocked()
    try:
        body_after = (await page.locator("body").inner_text(timeout=2500)).lower()
        still_prompt = any(marker in body_after for marker in prompt_markers)
    except Exception:
        still_prompt = False
    return {"attempted": True, "submitted": True, "prompt_still_visible": still_prompt, "url": page.url}


# Run PIN recovery automatically after every navigation performed by the bridge.
_original_goto = messenger._goto_unlocked


async def _goto_with_message_pin(url: str):
    page = await _original_goto(url)
    await _try_message_pin(page)
    return page


messenger._goto_unlocked = _goto_with_message_pin


async def _filtered_snapshot(page, max_text_lines: int = 100, max_elements: int = 180) -> dict[str, Any]:
    """Return a compact visible DOM/ARIA snapshot, never input values or cookies."""
    data = await page.evaluate(
        """([maxTextLines, maxElements]) => {
          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
          const visible = el => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 1 && r.height > 1 && st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity || 1) > 0;
          };
          const lines = [];
          const seenLines = new Set();
          const bodyText = (document.body && document.body.innerText) || '';
          for (const raw of bodyText.split(/\\n+/)) {
            const t = clean(raw);
            if (!t || seenLines.has(t)) continue;
            seenLines.add(t);
            lines.push(t.slice(0, 500));
            if (lines.length >= maxTextLines) break;
          }

          const selector = 'a[href],button,input,textarea,select,[role],[contenteditable="true"]';
          const els = Array.from(document.querySelectorAll(selector));
          const out = [];
          const seen = new Set();
          for (const el of els) {
            if (out.length >= maxElements || !visible(el)) continue;
            const role = clean(el.getAttribute('role')) || ({A:'link',BUTTON:'button',INPUT:'textbox',TEXTAREA:'textbox',SELECT:'combobox'}[el.tagName] || '');
            const label = clean(el.getAttribute('aria-label'));
            const placeholder = clean(el.getAttribute('placeholder'));
            const title = clean(el.getAttribute('title'));
            const text = clean(el.innerText || el.textContent).slice(0, 350);
            const href = el.tagName === 'A' ? (el.href || '') : '';
            const type = clean(el.getAttribute('type'));
            const key = [role, label, placeholder, title, text, href].join('|');
            if (!key.replace(/\\|/g,'')) continue;
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({
              id: out.length,
              tag: el.tagName.toLowerCase(),
              role,
              type,
              aria_label: label,
              placeholder,
              title,
              text,
              href: href.slice(0, 700)
            });
          }
          return {visible_text: lines, interactive_elements: out};
        }""",
        [max_text_lines, max_elements],
    )
    return {
        "url": page.url,
        "title": await page.title(),
        "visible_text": data.get("visible_text", []),
        "interactive_elements": data.get("interactive_elements", []),
        "note": "Filtered visible DOM/ARIA only. Input values, cookies, session data and PIN are excluded.",
    }


async def _inspect_messages() -> dict[str, Any]:
    async with messenger._lock:
        page = await messenger._goto_unlocked(MESSENGER_URL)
        await messenger._require_login(page)
        snapshot = await _filtered_snapshot(page)
        messenger._touch_unlocked()
        return snapshot


async def _adaptive_list_chats(limit: int) -> dict[str, Any]:
    """Use the normal extractor first; if empty, recover from the live filtered DOM."""
    limit = max(1, min(int(limit), 100))
    chats = await messenger.list_chats(limit)
    if chats:
        return {"chats": chats, "method": "facebook_link_extractor"}

    snapshot = await _inspect_messages()
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for el in snapshot.get("interactive_elements", []):
        href = str(el.get("href") or "")
        if not href or "facebook.com" not in href:
            continue
        path = urlparse(href).path
        if "/messages/t/" not in path and "/messages/e2ee/t/" not in path:
            continue
        canonical = f"https://www.facebook.com{path.rstrip('/')}/"
        if canonical in seen:
            continue
        seen.add(canonical)
        name = str(el.get("aria_label") or el.get("text") or "").strip()
        if not name:
            name = path.rstrip("/").rsplit("/", 1)[-1]
        candidates.append({"name": name[:200], "url": canonical, "preview": ""})
        if len(candidates) >= limit:
            break

    result: dict[str, Any] = {"chats": candidates, "method": "filtered_dom_fallback"}
    if not candidates:
        result["diagnostic"] = {
            "url": snapshot.get("url"),
            "title": snapshot.get("title"),
            "visible_text": snapshot.get("visible_text", [])[:45],
            "interactive_elements": snapshot.get("interactive_elements", [])[:80],
        }
    return result


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
        "description": "Check whether the Facebook Web Messages session is logged in. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "messenger_list_chats",
        "title": "List Messenger chats",
        "description": "List recent Messenger conversations from Facebook Web. If Facebook changes markup, automatically fall back to a filtered live DOM/ARIA scan. Read-only.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "messenger_read_chat",
        "title": "Read Messenger chat",
        "description": "Read recent visible messages from one Facebook Web Messages conversation. Read-only.",
        "inputSchema": {"type": "object", "properties": {"chat": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30}}, "required": ["chat"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "messenger_send_message",
        "title": "Send Messenger message",
        "description": "Send a Messenger message through Facebook Web. Call only when the user explicitly asks to send.",
        "inputSchema": {"type": "object", "properties": {"chat": {"type": "string"}, "message": {"type": "string", "minLength": 1, "maxLength": 4000}}, "required": ["chat", "message"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "browser_inspect",
        "title": "Inspect Facebook Messages UI",
        "description": "Read a filtered snapshot of the currently visible Facebook Messages UI: URL, title, visible text, links, buttons and form controls. Never returns input values, cookies, PINs or session data. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "messenger_unlock_history",
        "title": "Unlock encrypted message history",
        "description": "If Facebook is visibly asking for the Messenger secure-storage PIN, submit the PIN stored privately on the server. The PIN is never returned to ChatGPT.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
]


async def _call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "messenger_status":
            return _tool_text(await messenger.status())
        if name == "messenger_list_chats":
            return _tool_text(await _adaptive_list_chats(args.get("limit", 20)))
        if name == "messenger_read_chat":
            return _tool_text(await messenger.read_chat(args["chat"], args.get("limit", 30)))
        if name == "messenger_send_message":
            return _tool_text(await messenger.send_message(args["chat"], args["message"]))
        if name == "browser_inspect":
            return _tool_text(await _inspect_messages())
        if name == "messenger_unlock_history":
            async with messenger._lock:
                page = await messenger._goto_unlocked(MESSENGER_URL)
                result = await _try_message_pin(page)
                return _tool_text(result, bool(result.get("attempted") and result.get("prompt_still_visible")))
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
        "site": "Facebook Web Messages",
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
            "serverInfo": {"name": "personal-messenger-render-bridge", "version": "0.7.0"},
            "instructions": "Use Facebook Web Messages. When normal extraction fails, inspect the filtered DOM/ARIA snapshot instead of guessing selectors. If a secure-storage PIN prompt is visible, call messenger_unlock_history; never request or reveal the PIN. Send messages only when explicitly requested. Never reveal session data, cookies, PINs or access keys.",
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
