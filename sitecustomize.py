from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlparse

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    access_key = os.getenv("MCP_ACCESS_KEY", "").strip()
    encrypted_path = Path(__file__).resolve().with_name("default_facebook_session.enc")

    if access_key and encrypted_path.exists():
        key = hashlib.sha256(access_key.encode("utf-8")).digest()
        packed = base64.urlsafe_b64decode(encrypted_path.read_bytes().strip())
        nonce, ciphertext = packed[:12], packed[12:]
        compressed = AESGCM(key).decrypt(nonce, ciphertext, b"facebook-session-v1")
        decrypted = zlib.decompress(compressed)
        state = json.loads(decrypted.decode("utf-8"))
        if isinstance(state, dict) and isinstance(state.get("cookies"), list):
            os.environ["MESSENGER_STORAGE_STATE_B64"] = base64.b64encode(decrypted).decode("ascii")
            os.environ.setdefault("FACEBOOK_MESSAGES_URL", "https://www.facebook.com/messages/")
            fp = hashlib.sha256(access_key.encode("utf-8")).hexdigest()[:12]
            print(f"[bootstrap] encrypted default Facebook session loaded keyfp={fp}", flush=True)
except Exception as exc:
    fp = hashlib.sha256(os.getenv("MCP_ACCESS_KEY", "").strip().encode("utf-8")).hexdigest()[:12]
    print(f"[bootstrap] default Facebook session unavailable: {type(exc).__name__} keyfp={fp}", flush=True)


if os.getenv("PORT"):
    try:
        import browser_bridge as _bb

        async def _maybe_submit_message_pin(page):
            pin = os.getenv("FACEBOOK_MESSAGE_PIN", "").strip()
            if not pin:
                return False
            try:
                state = await page.evaluate(
                    """() => {
                      const t = ((document.body && document.body.innerText) || '').toLowerCase();
                      return {
                        pinPrompt: ['enter your pin','enter pin','nhập mã pin','nhập pin','restore chat history','restore your chats','khôi phục lịch sử','mã pin để khôi phục'].some(x => t.includes(x)),
                        login: t.includes('log into facebook') || t.includes('đăng nhập facebook')
                      };
                    }"""
                )
            except Exception:
                return False
            if not state.get("pinPrompt") or state.get("login"):
                return False

            target = None
            for selector in (
                'input[autocomplete="one-time-code"]',
                'input[inputmode="numeric"]',
                'input[aria-label*="PIN" i]',
                'input[placeholder*="PIN" i]',
                'input[type="password"]',
            ):
                try:
                    loc = page.locator(selector)
                    count = min(await loc.count(), 6)
                    for i in range(count):
                        candidate = loc.nth(i)
                        if await candidate.is_visible():
                            target = candidate
                            break
                except Exception:
                    continue
                if target is not None:
                    break
            if target is None:
                return False
            try:
                await target.fill(pin)
            except Exception:
                return False
            for label in ("Continue", "Tiếp tục", "Confirm", "Xác nhận", "Restore", "Khôi phục", "Done", "Xong"):
                try:
                    b = page.get_by_role("button", name=label, exact=False)
                    if await b.count() and await b.first.is_visible():
                        await b.first.click()
                        await page.wait_for_timeout(1400)
                        print("[pin] secure-storage PIN submitted", flush=True)
                        return True
                except Exception:
                    continue
            try:
                await target.press("Enter")
                await page.wait_for_timeout(1400)
                print("[pin] secure-storage PIN submitted with Enter", flush=True)
                return True
            except Exception:
                return False

        async def _waited_list_chats(self, limit: int = 20):
            limit = max(1, min(int(limit), 100))
            async with self._lock:
                page = await self._goto_unlocked(_bb.MESSENGER_URL)
                await self._require_login(page)
                found: dict[str, dict[str, str]] = {}
                last_loading = False
                for attempt in range(26):
                    try:
                        raw = await page.evaluate(
                            """() => {
                              const root = document.querySelector('[role="navigation"][aria-label="Thread list"]') || document;
                              const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
                              const rows = [];
                              const seen = new Set();
                              for (const a of root.querySelectorAll('a[href]')) {
                                const href = a.href || '';
                                if (!href.includes('/messages/t/') && !href.includes('/messages/e2ee/t/')) continue;
                                if (seen.has(href)) continue;
                                seen.add(href);
                                rows.push({href, text: clean(a.innerText || a.textContent).slice(0, 500), aria: clean(a.getAttribute('aria-label')).slice(0, 300)});
                              }
                              const loading = !!root.querySelector('[role="status"][aria-label="Loading..."]') || !!document.querySelector('[role="status"][aria-label="Loading..."]');
                              return {rows, loading};
                            }"""
                        )
                    except Exception:
                        raw = {"rows": [], "loading": False}
                    last_loading = bool(raw.get("loading"))
                    for item in raw.get("rows") or []:
                        href = str(item.get("href") or "")
                        parsed = urlparse(href)
                        path = parsed.path.rstrip("/") + "/"
                        if "/messages/t/" not in path and "/messages/e2ee/t/" not in path:
                            continue
                        canonical = f"https://www.facebook.com{path}"
                        if canonical in found:
                            continue
                        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
                        aria = re.sub(r"\s+", " ", str(item.get("aria") or "")).strip()
                        name = aria or text or path.rstrip("/").rsplit("/", 1)[-1]
                        found[canonical] = {"name": name[:200], "url": canonical, "preview": text[:300]}
                        if len(found) >= limit:
                            break
                    if found or (attempt >= 5 and not last_loading):
                        break
                    await page.wait_for_timeout(1000)
                print(f"[thread-list] found={len(found)} loading={last_loading} url={page.url}", flush=True)
                self._touch_unlocked()
                return list(found.values())[:limit]

        async def _conversation_snapshot(page, limit: int):
            try:
                return await page.evaluate(
                    """(limit) => {
                      const clean = s => (s || '').replace(/[ \\t]+/g, ' ').replace(/\\n{3,}/g, '\\n\\n').trim();
                      const visible = el => {
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden';
                      };
                      const main = document.querySelector('[role="main"]') || document.body;
                      const composers = Array.from(document.querySelectorAll('[role="textbox"][contenteditable="true"], div[contenteditable="true"]')).filter(visible);
                      const candidates = Array.from(main.querySelectorAll('[role="row"], [data-scope="messages_table"] [role="row"], div[dir="auto"]')).filter(visible);
                      const texts = [];
                      const seen = new Set();
                      for (const el of candidates) {
                        const t = clean(el.innerText || el.textContent);
                        if (!t || t.length > 4000 || seen.has(t)) continue;
                        seen.add(t);
                        texts.push(t);
                      }
                      const body = clean((document.body && document.body.innerText) || '');
                      return {
                        composerCount: composers.length,
                        nodeCount: candidates.length,
                        messages: texts.slice(-Math.max(limit * 4, 40)).slice(-limit),
                        mainText: clean(main.innerText || '').slice(0, 2500),
                        bodyLower: body.toLowerCase().slice(0, 6000)
                      };
                    }""",
                    limit,
                )
            except Exception:
                return {"composerCount": 0, "nodeCount": 0, "messages": [], "mainText": "", "bodyLower": ""}

        async def _wait_conversation_ready(page, limit: int):
            last = {}
            pin_attempted = False
            for attempt in range(27):
                last = await _conversation_snapshot(page, limit)
                if last.get("composerCount", 0) > 0 or last.get("nodeCount", 0) > 5:
                    return last
                if not pin_attempted and attempt >= 2:
                    pin_attempted = await _maybe_submit_message_pin(page)
                await page.wait_for_timeout(900)
            return last

        async def _read_chat_light(self, chat: str, limit: int = 30):
            limit = max(1, min(int(limit), 100))
            url, resolved_name = await self._resolve_chat(chat)
            async with self._lock:
                page = await self._goto_unlocked(url)
                await self._require_login(page)
                snap = await _wait_conversation_ready(page, limit)
                messages = [str(x).strip() for x in (snap.get("messages") or []) if str(x).strip()]
                skip = {"Messenger", "Chats", "Search Messenger", "New message", "Đoạn chat", "Tìm kiếm trên Messenger"}
                cleaned = [x for x in messages if x not in skip]
                self._touch_unlocked()
                return {
                    "chat": resolved_name,
                    "url": page.url,
                    "messages": cleaned[-limit:],
                    "site": "Facebook Web Messages",
                    "composer_present": int(snap.get("composerCount", 0)) > 0,
                    "composer_count": int(snap.get("composerCount", 0)),
                    "ui_message_nodes": int(snap.get("nodeCount", 0)),
                    "send_dry_run": "ready" if int(snap.get("composerCount", 0)) > 0 else "composer_not_found",
                    "ui_probe": [] if cleaned else [x.strip()[:500] for x in str(snap.get("mainText") or "").splitlines() if x.strip()][:20],
                    "warning": "Facebook Web DOM is unofficial and may change.",
                }

        async def _send_message_light(self, chat: str, message: str):
            message = message.strip()
            if not message:
                raise ValueError("message không được để trống")
            if len(message) > 4000:
                raise ValueError("message quá dài (>4000 ký tự)")
            url, resolved_name = await self._resolve_chat(chat)
            async with self._lock:
                page = await self._goto_unlocked(url)
                await self._require_login(page)
                snap = await _wait_conversation_ready(page, 5)
                if int(snap.get("composerCount", 0)) <= 0:
                    raise RuntimeError("Không tìm thấy ô soạn tin sau khi đã chờ Facebook tải hội thoại.")
                composer = page.locator('[role="textbox"][contenteditable="true"]')
                if await composer.count() == 0:
                    composer = page.locator('div[contenteditable="true"]')
                if await composer.count() == 0:
                    raise RuntimeError("Không tìm thấy ô soạn tin trên Facebook Web.")
                box = composer.last
                await box.click()
                try:
                    await box.fill(message)
                except Exception:
                    await page.keyboard.type(message, delay=10)
                await box.press("Enter")
                await page.wait_for_timeout(900)
                self._touch_unlocked()
                return {"sent": True, "chat": resolved_name, "text": message, "url": page.url, "site": "Facebook Web Messages"}

        _bb.MessengerBrowser.list_chats = _waited_list_chats
        _bb.MessengerBrowser.read_chat = _read_chat_light
        _bb.MessengerBrowser.send_message = _send_message_light
        print("[runtime-patch] lightweight list/read/send extractors installed", flush=True)
    except Exception as exc:
        print(f"[runtime-patch] failed: {type(exc).__name__}: {exc}", flush=True)


def _selfdiag_call(tool: str, arguments: dict | None = None):
    key = os.getenv("MCP_ACCESS_KEY", "").strip()
    port = os.getenv("PORT", "").strip()
    if not key or not port:
        return None
    url = f"http://127.0.0.1:{port}/mcp/{key}"
    payload = {"jsonrpc": "2.0", "id": f"selfdiag-{tool}", "method": "tools/call", "params": {"name": tool, "arguments": arguments or {}}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=100) as r:
        outer = json.loads(r.read().decode("utf-8"))
    return (outer.get("result") or {}).get("structuredContent") or outer.get("result")


def _run_selfdiag() -> None:
    try:
        time.sleep(14)
        listing = _selfdiag_call("messenger_list_chats", {"limit": 10}) or {}
        chats = listing.get("chats") or []
        print("[selftest-list] " + json.dumps({"count": len(chats), "method": listing.get("method")}, ensure_ascii=False), flush=True)
        probe_chat = next((c for c in chats if "/messages/e2ee/t/" in str(c.get("url") or "")), chats[0] if chats else None)
        if probe_chat:
            print("[selftest-probe-chat] " + json.dumps({"name": probe_chat.get("name"), "url": probe_chat.get("url")}, ensure_ascii=False), flush=True)
            probe = _selfdiag_call("messenger_read_chat", {"chat": probe_chat.get("url"), "limit": 5}) or {}
            print("[selftest-read-send] " + json.dumps({
                "url": probe.get("url"),
                "message_count": len(probe.get("messages") or []),
                "composer_present": probe.get("composer_present"),
                "composer_count": probe.get("composer_count"),
                "ui_message_nodes": probe.get("ui_message_nodes"),
                "send_dry_run": probe.get("send_dry_run"),
                "ui_probe": probe.get("ui_probe"),
                "error": probe.get("error"),
            }, ensure_ascii=False)[:12000], flush=True)
    except Exception as exc:
        print(f"[selftest-failed] {type(exc).__name__}: {exc}", flush=True)


if os.getenv("PORT") and os.getenv("MCP_ACCESS_KEY"):
    threading.Thread(target=_run_selfdiag, name="messenger-selfdiag", daemon=True).start()
