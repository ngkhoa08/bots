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

        _original_read_chat = _bb.MessengerBrowser.read_chat

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

        async def _probe_conversation_ui(page):
            composer = page.locator('[role="textbox"][contenteditable="true"], div[contenteditable="true"]')
            composer_count = 0
            message_count = 0
            for _ in range(26):
                try:
                    composer_count = await composer.count()
                    message_count = max(
                        await page.locator('[role="main"] [role="row"]').count(),
                        await page.locator('[role="main"] div[dir="auto"]').count(),
                    )
                except Exception:
                    composer_count = 0
                    message_count = 0
                if composer_count > 0 or message_count > 3:
                    break
                await page.wait_for_timeout(1000)
            return composer_count, message_count

        async def _extract_messages_after_wait(page, limit: int):
            selectors = [
                '[role="main"] [role="row"]',
                '[role="main"] [data-scope="messages_table"] [role="row"]',
                '[role="main"] div[dir="auto"]',
            ]
            raw: list[str] = []
            for sel in selectors:
                loc = page.locator(sel)
                try:
                    count = await loc.count()
                except Exception:
                    continue
                if count == 0:
                    continue
                start = max(0, count - max(limit * 5, 80))
                for i in range(start, count):
                    try:
                        text = (await loc.nth(i).inner_text()).strip()
                    except Exception:
                        continue
                    text = re.sub(r"[ \t]+", " ", text)
                    text = re.sub(r"\n{3,}", "\n\n", text).strip()
                    if text and len(text) <= 4000:
                        raw.append(text)
                if raw:
                    break
            cleaned: list[str] = []
            skip = {"Messenger", "Chats", "Search Messenger", "New message", "Đoạn chat", "Tìm kiếm trên Messenger"}
            for text in raw:
                if cleaned and text == cleaned[-1]:
                    continue
                if text in skip:
                    continue
                cleaned.append(text)
            return cleaned[-limit:]

        async def _read_chat_waited(self, chat: str, limit: int = 30):
            limit = max(1, min(int(limit), 100))
            result = await _original_read_chat(self, chat, limit)
            composer_count = 0
            ui_message_count = 0
            try:
                async with self._lock:
                    page = self._page
                    if page is not None:
                        composer_count, ui_message_count = await _probe_conversation_ui(page)
                        messages = await _extract_messages_after_wait(page, limit)
                        if isinstance(result, dict) and messages:
                            result["messages"] = messages
                        if isinstance(result, dict) and composer_count == 0:
                            try:
                                body = (await page.locator("body").inner_text()).splitlines()
                                result["ui_probe"] = [re.sub(r"\s+", " ", x).strip()[:400] for x in body if x.strip()][:25]
                            except Exception:
                                pass
            except Exception:
                pass
            if isinstance(result, dict):
                result["composer_present"] = composer_count > 0
                result["composer_count"] = composer_count
                result["ui_message_nodes"] = ui_message_count
                result["send_dry_run"] = "ready" if composer_count > 0 else "composer_not_found"
            return result

        async def _send_message_waited(self, chat: str, message: str):
            message = message.strip()
            if not message:
                raise ValueError("message không được để trống")
            if len(message) > 4000:
                raise ValueError("message quá dài (>4000 ký tự)")
            url, resolved_name = await self._resolve_chat(chat)
            async with self._lock:
                page = await self._goto_unlocked(url)
                await self._require_login(page)
                composer_count, _ = await _probe_conversation_ui(page)
                if composer_count <= 0:
                    raise RuntimeError("Không tìm thấy ô soạn tin sau khi đã chờ Facebook tải hội thoại.")
                composer = page.locator('[role="textbox"][contenteditable="true"]')
                count = await composer.count()
                if count == 0:
                    composer = page.locator('div[contenteditable="true"]')
                    count = await composer.count()
                if count == 0:
                    raise RuntimeError("Không tìm thấy ô soạn tin trên Facebook Web.")
                box = composer.last
                await box.click()
                try:
                    await box.fill(message)
                except Exception:
                    await page.keyboard.type(message)
                await box.press("Enter")
                await page.wait_for_timeout(900)
                self._touch_unlocked()
                return {"sent": True, "chat": resolved_name, "text": message, "url": page.url, "site": "Facebook Web Messages"}

        _bb.MessengerBrowser.list_chats = _waited_list_chats
        _bb.MessengerBrowser.read_chat = _read_chat_waited
        _bb.MessengerBrowser.send_message = _send_message_waited
        print("[runtime-patch] waited list/read/send extractors installed", flush=True)
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
