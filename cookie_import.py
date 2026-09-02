from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from browser_bridge import MESSENGER_URL, messenger

COOKIE_HTML = r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Import Messenger Cookie</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#eee;margin:0;padding:20px}main{max-width:850px;margin:auto}.card{background:#1c1c1c;border:1px solid #333;border-radius:14px;padding:16px;margin-bottom:14px}textarea{width:100%;box-sizing:border-box;min-height:270px;background:#0c0c0c;color:#eee;border:1px solid #555;border-radius:10px;padding:12px;font-family:ui-monospace,monospace;font-size:13px;resize:vertical}button{padding:11px 15px;border:0;border-radius:9px;font-weight:700;cursor:pointer;margin-right:8px}#go{background:#fff;color:#111}#msg{white-space:pre-wrap;margin-top:12px}.warn{color:#ffc267}.ok{color:#76df99}small{color:#aaa;line-height:1.5}code{background:#292929;padding:2px 5px;border-radius:5px}</style></head>
<body><main><div class="card"><h2>Đăng nhập Messenger bằng cookie</h2>
<p class="warn">Cookie là chìa khóa đăng nhập tài khoản. Chỉ dán vào trang Render này; không gửi cookie vào ChatGPT hoặc cho người khác.</p>
<small>Trang này chỉ nạp cookie vào Chromium rồi mở <code>https://www.messenger.com/</code>. Không mở Facebook trước.</small></div>
<div class="card"><textarea id="cookie" autocomplete="off" spellcheck="false" placeholder='Dán cookie JSON hoặc Cookie header vào đây'></textarea><div style="margin-top:10px"><button id="go">Nạp cookie & mở Messenger</button><button id="clear">Xóa ô</button></div><div id="msg"></div></div>
</main><script>
const base=location.pathname.replace(/\/$/,'');const box=document.getElementById('cookie'),msg=document.getElementById('msg');
document.getElementById('clear').onclick=()=>{box.value='';msg.textContent=''};
document.getElementById('go').onclick=async()=>{const value=box.value.trim();if(!value){msg.textContent='Chưa có cookie.';return}msg.textContent='Đang nạp cookie và mở Messenger...';try{const r=await fetch(base+'/apply',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({cookie:value})});const text=await r.text();let j;try{j=JSON.parse(text)}catch{throw new Error('Render vừa ngắt kết nối hoặc trả về trang HTML. Hãy chờ vài giây rồi thử lại.')}if(!r.ok)throw new Error(j.detail||j.error||('HTTP '+r.status));box.value='';if(j.logged_in){msg.innerHTML='<span class="ok">✓ Đăng nhập Messenger thành công. Session đã được lưu trên Render.</span><br>'+j.url}else{msg.innerHTML='<span class="warn">Cookie đã được nạp nhưng Messenger chưa xác nhận đăng nhập.</span><br>'+j.url+'<br>Cookie có thể thiếu, hết hạn hoặc Messenger yêu cầu xác minh.'}}catch(e){msg.textContent='Lỗi: '+e.message}};
</script></body></html>'''


class CookieBody(BaseModel):
    cookie: str = Field(min_length=1, max_length=200_000)


def _same_site(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"no_restriction", "none"}:
        return "None"
    if s == "lax":
        return "Lax"
    if s == "strict":
        return "Strict"
    return None


def _from_json(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
        raw = raw["cookies"]
    if not isinstance(raw, list):
        raise ValueError("JSON cookie phải là một mảng")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        value = str(item.get("value", ""))
        domain = str(item.get("domain", "")).strip()
        path = str(item.get("path", "/") or "/")
        c: dict[str, Any] = {"name": name, "value": value, "path": path}
        if domain:
            c["domain"] = domain
        else:
            c["url"] = MESSENGER_URL
        exp = item.get("expirationDate", item.get("expires"))
        try:
            if exp is not None and float(exp) > 0:
                c["expires"] = float(exp)
        except Exception:
            pass
        if "httpOnly" in item:
            c["httpOnly"] = bool(item.get("httpOnly"))
        if "secure" in item:
            c["secure"] = bool(item.get("secure"))
        ss = _same_site(item.get("sameSite"))
        if ss:
            c["sameSite"] = ss
        out.append(c)
    if not out:
        raise ValueError("Không tìm thấy cookie hợp lệ trong JSON")
    return out


def _from_header(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    pairs: list[tuple[str, str]] = []
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    if not pairs:
        raise ValueError("Cookie header không hợp lệ")
    return [{"name": n, "value": v, "domain": ".messenger.com", "path": "/", "secure": True} for n, v in pairs]


def parse_cookie(text: str) -> list[dict[str, Any]]:
    s = text.strip()
    if s.startswith("[") or s.startswith("{"):
        try:
            return _from_json(json.loads(s))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON cookie không hợp lệ") from exc
    return _from_header(s)


def register_cookie_import(app: FastAPI, access_key: str) -> None:
    def valid(key: str) -> bool:
        return bool(access_key) and hmac.compare_digest(key, access_key)

    @app.get("/cookie/{key}", response_class=HTMLResponse)
    async def cookie_ui(key: str) -> HTMLResponse:
        if not valid(key):
            raise HTTPException(401, "Unauthorized")
        return HTMLResponse(COOKIE_HTML, headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})

    @app.post("/cookie/{key}/apply")
    async def cookie_apply(key: str, body: CookieBody) -> JSONResponse:
        if not valid(key):
            raise HTTPException(401, "Unauthorized")
        try:
            cookies = parse_cookie(body.cookie)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        async with messenger._lock:
            await messenger._close_unlocked()
            await messenger._start_unlocked()
            assert messenger._context is not None
            assert messenger._page is not None
            await messenger._context.clear_cookies()
            try:
                await messenger._context.add_cookies(cookies)
            except Exception as exc:
                raise HTTPException(400, f"Playwright không chấp nhận cookie: {exc}")

            page = messenger._page
            await page.goto(MESSENGER_URL, wait_until="domcontentloaded", timeout=35_000)
            await page.wait_for_timeout(1600)
            logged = await messenger._is_logged_in(page)
            if logged:
                await messenger._persist_runtime_state_unlocked()
            messenger._touch_unlocked()
            return JSONResponse({"ok": True, "logged_in": logged, "url": page.url, "cookie_count": len(cookies)})
