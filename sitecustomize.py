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


# Runtime-only monkey patches. Facebook renders the Thread list asynchronously on
# a very small Render CPU; the old extractor gave up after only a few seconds and
# then the fallback reloaded the page, resetting the list to Loading... again.
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
                # 25 seconds is intentional: Render free has only 0.1 CPU and
                # Facebook's React/Messenger bundle can take a while to hydrate.
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
                                rows.push({
                                  href,
                                  text: clean(a.innerText || a.textContent).slice(0, 500),
                                  aria: clean(a.getAttribute('aria-label')).slice(0, 300)
                                });
                              }
                              const loading = !!root.querySelector('[role="status"][aria-label="Loading..."]') ||
                                              !!document.querySelector('[role="status"][aria-label="Loading..."]');
                              return {rows, loading, rootText: clean(root.innerText || '').slice(0, 1200)};
                            }"""
                        )
                    except Exception:
                        raw = {"rows": [], "loading": False, "rootText": ""}

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
                        lines = [x.strip() for x in text.split(" · ") if x.strip()]
                        name = aria or (lines[0] if lines else text)
                        if not name:
                            name = path.rstrip("/").rsplit("/", 1)[-1]
                        preview = text[:300]
                        found[canonical] = {"name": name[:200], "url": canonical, "preview": preview}
                        if len(found) >= limit:
                            break

                    if found or (attempt >= 5 and not last_loading):
                        break
                    await page.wait_for_timeout(1000)

                print(
                    f"[thread-list] found={len(found)} loading={last_loading} url={page.url}",
                    flush=True,
                )
                self._touch_unlocked()
                return list(found.values())[:limit]

        async def _read_chat_with_composer_probe(self, chat: str, limit: int = 30):
            result = await _original_read_chat(self, chat, limit)
            composer_count = 0
            try:
                async with self._lock:
                    if self._page is not None:
                        composer = self._page.locator('[role="textbox"][contenteditable="true"], div[contenteditable="true"]')
                        composer_count = await composer.count()
            except Exception:
                composer_count = 0
            if isinstance(result, dict):
                result["composer_present"] = composer_count > 0
                result["composer_count"] = composer_count
                result["send_dry_run"] = "ready" if composer_count > 0 else "composer_not_found"
            return result

        _bb.MessengerBrowser.list_chats = _waited_list_chats
        _bb.MessengerBrowser.read_chat = _read_chat_with_composer_probe
        print("[runtime-patch] waited Thread-list extractor installed", flush=True)
    except Exception as exc:
        print(f"[runtime-patch] failed: {type(exc).__name__}: {exc}", flush=True)


def _selfdiag_call(tool: str, arguments: dict | None = None):
    key = os.getenv("MCP_ACCESS_KEY", "").strip()
    port = os.getenv("PORT", "").strip()
    if not key or not port:
        return None
    url = f"http://127.0.0.1:{port}/mcp/{key}"
    payload = {
        "jsonrpc": "2.0",
        "id": f"selfdiag-{tool}",
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        outer = json.loads(r.read().decode("utf-8"))
    return (outer.get("result") or {}).get("structuredContent") or outer.get("result")


def _run_selfdiag() -> None:
    try:
        time.sleep(14)
        listing = _selfdiag_call("messenger_list_chats", {"limit": 10}) or {}
        chats = listing.get("chats") or []
        print(
            "[selftest-list] " + json.dumps({
                "count": len(chats),
                "method": listing.get("method"),
                "diagnostic_url": (listing.get("diagnostic") or {}).get("url"),
            }, ensure_ascii=False),
            flush=True,
        )
        for chat in chats[:10]:
            print("[selftest-chat] " + json.dumps({
                "name": chat.get("name"),
                "url": chat.get("url"),
            }, ensure_ascii=False), flush=True)

        if chats:
            probe = _selfdiag_call("messenger_read_chat", {"chat": chats[0].get("url"), "limit": 3}) or {}
            print(
                "[selftest-read-send] " + json.dumps({
                    "url": probe.get("url"),
                    "message_count": len(probe.get("messages") or []),
                    "composer_present": probe.get("composer_present"),
                    "composer_count": probe.get("composer_count"),
                    "send_dry_run": probe.get("send_dry_run"),
                    "error": probe.get("error"),
                }, ensure_ascii=False),
                flush=True,
            )
    except Exception as exc:
        print(f"[selftest-failed] {type(exc).__name__}: {exc}", flush=True)


if os.getenv("PORT") and os.getenv("MCP_ACCESS_KEY"):
    threading.Thread(target=_run_selfdiag, name="messenger-selfdiag", daemon=True).start()
