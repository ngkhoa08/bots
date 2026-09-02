from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route, async_playwright

RUNTIME_STATE = Path(os.getenv("RUNTIME_STATE_PATH", "/tmp/messenger_runtime_state.json"))
# Intentionally use Facebook Web instead of messenger.com.
# A separate env name avoids an old MESSENGER_URL env var silently forcing messenger.com.
MESSENGER_URL = os.getenv("FACEBOOK_MESSAGES_URL", "https://www.facebook.com/messages/")
FACEBOOK_ORIGIN = "https://www.facebook.com"
HEADLESS = os.getenv("HEADLESS", "true").lower() not in {"0", "false", "no"}
BLOCK_HEAVY = os.getenv("BLOCK_HEAVY_RESOURCES", "true").lower() not in {"0", "false", "no"}
IDLE_SECONDS = max(20, int(os.getenv("BROWSER_IDLE_SECONDS", "90")))

CHAT_SELECTOR = 'a[href*="/messages/t/"], a[href*="/messages/e2ee/t/"]'


def _sanitize_storage_state(raw: Any) -> dict[str, Any] | None:
    """Keep only cookies + localStorage and deliberately exclude IndexedDB."""
    if not isinstance(raw, dict):
        return None

    cookies = raw.get("cookies")
    if not isinstance(cookies, list):
        cookies = []

    clean_origins: list[dict[str, Any]] = []
    origins = raw.get("origins")
    if isinstance(origins, list):
        for item in origins:
            if not isinstance(item, dict):
                continue
            origin = item.get("origin")
            if not isinstance(origin, str) or not origin:
                continue
            local_out: list[dict[str, str]] = []
            local = item.get("localStorage")
            if isinstance(local, list):
                for entry in local:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    value = entry.get("value")
                    if isinstance(name, str) and isinstance(value, str):
                        local_out.append({"name": name, "value": value})
            clean_origins.append({"origin": origin, "localStorage": local_out})

    return {"cookies": cookies, "origins": clean_origins}


def _normalize_facebook_chat_url(href: str) -> str | None:
    """Normalize Facebook/Messenger conversation URLs to Facebook Web messages."""
    href = href.strip()
    if not href:
        return None

    full = urljoin(FACEBOOK_ORIGIN, href)
    parsed = urlparse(full)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") + "/"

    if host in {"facebook.com", "www.facebook.com"}:
        if "/messages/e2ee/t/" in path or "/messages/t/" in path:
            return f"{FACEBOOK_ORIGIN}{path}"
        return None

    # Accept old messenger.com links and translate them to Facebook Web.
    if host in {"messenger.com", "www.messenger.com"}:
        if "/e2ee/t/" in path:
            tail = path.split("/e2ee/t/", 1)[1]
            return f"{FACEBOOK_ORIGIN}/messages/e2ee/t/{tail}"
        if "/t/" in path:
            tail = path.split("/t/", 1)[1]
            return f"{FACEBOOK_ORIGIN}/messages/t/{tail}"
    return None


class MessengerBrowser:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._last_used = 0.0

    def _initial_state(self) -> dict[str, Any] | None:
        if RUNTIME_STATE.exists():
            try:
                raw = json.loads(RUNTIME_STATE.read_text(encoding="utf-8"))
                clean = _sanitize_storage_state(raw)
                if clean is not None:
                    RUNTIME_STATE.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
                    return clean
            except Exception:
                try:
                    RUNTIME_STATE.unlink(missing_ok=True)
                except Exception:
                    pass

        encoded = os.getenv("MESSENGER_STORAGE_STATE_B64", "").strip()
        if not encoded:
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True)
            raw = json.loads(decoded.decode("utf-8"))
            return _sanitize_storage_state(raw)
        except Exception as exc:
            raise RuntimeError("MESSENGER_STORAGE_STATE_B64 không hợp lệ") from exc

    async def _route(self, route: Route) -> None:
        req = route.request
        if BLOCK_HEAVY and req.resource_type in {"image", "media", "font"}:
            await route.abort()
            return
        host = urlparse(req.url).hostname or ""
        if BLOCK_HEAVY and any(x in host for x in ("doubleclick.net", "googletagmanager.com")):
            await route.abort()
            return
        await route.continue_()

    async def _start_unlocked(self) -> None:
        if self._context is not None:
            self._touch_unlocked()
            return

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--mute-audio",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--no-first-run",
                "--no-default-browser-check",
                "--renderer-process-limit=2",
                "--js-flags=--max-old-space-size=256",
                "--disable-features=BackForwardCache,MediaRouter,OptimizationHints,Translate",
            ],
        )

        kwargs: dict[str, Any] = {
            "viewport": {"width": 1024, "height": 720},
            "locale": "vi-VN",
        }
        state = self._initial_state()
        if state:
            kwargs["storage_state"] = state

        try:
            self._context = await self._browser.new_context(**kwargs)
        except Exception:
            if "storage_state" not in kwargs:
                raise
            try:
                RUNTIME_STATE.unlink(missing_ok=True)
            except Exception:
                pass
            kwargs.pop("storage_state", None)
            self._context = await self._browser.new_context(**kwargs)

        await self._context.route("**/*", self._route)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(15_000)
        self._touch_unlocked()

    async def _persist_runtime_state_unlocked(self) -> None:
        if not self._context:
            return
        try:
            state = await self._context.storage_state()
            clean = _sanitize_storage_state(state)
            if clean is None:
                return
            RUNTIME_STATE.parent.mkdir(parents=True, exist_ok=True)
            RUNTIME_STATE.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    async def _close_unlocked(self) -> None:
        if self._idle_task:
            current = asyncio.current_task()
            if self._idle_task is not current:
                self._idle_task.cancel()
            self._idle_task = None
        await self._persist_runtime_state_unlocked()
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._page = None
        self._pw = None

    def _touch_unlocked(self) -> None:
        self._last_used = time.monotonic()
        if self._idle_task:
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_closer())

    async def _idle_closer(self) -> None:
        try:
            await asyncio.sleep(IDLE_SECONDS)
            async with self._lock:
                if self._context and time.monotonic() - self._last_used >= IDLE_SECONDS:
                    await self._close_unlocked()
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _get_page_unlocked(self) -> Page:
        await self._start_unlocked()
        assert self._page is not None
        return self._page

    async def _goto_unlocked(self, url: str) -> Page:
        page = await self._get_page_unlocked()
        await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
        await page.wait_for_timeout(1400)
        self._touch_unlocked()
        return page

    async def _is_logged_in(self, page: Page) -> bool:
        url = page.url.lower()
        if any(x in url for x in ("/login", "/checkpoint", "/recover", "/identify")):
            return False
        try:
            if await page.locator('input[name="email"], input[name="pass"], input[type="password"]').count():
                return False
        except Exception:
            pass

        # c_user is the most reliable indicator for a Facebook personal session.
        if self._context is not None:
            try:
                cookies = await self._context.cookies(FACEBOOK_ORIGIN)
                if any(c.get("name") == "c_user" and c.get("value") for c in cookies):
                    return True
            except Exception:
                pass

        try:
            if await page.locator(CHAT_SELECTOR).count() > 0:
                return True
        except Exception:
            pass
        return "facebook.com" in url and "/login" not in url

    async def _require_login(self, page: Page) -> None:
        if not await self._is_logged_in(page):
            raise RuntimeError(
                "Facebook chưa đăng nhập hoặc session đã hết hạn. "
                "Hãy nhập lại cookie facebook.com tại trang /cookie/<key>."
            )

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            page = await self._goto_unlocked(MESSENGER_URL)
            logged_in = await self._is_logged_in(page)
            return {
                "logged_in": logged_in,
                "url": page.url,
                "site": "Facebook Web Messages",
                "browser_idle_close_seconds": IDLE_SECONDS,
                "heavy_resources_blocked": BLOCK_HEAVY,
                "storage_state": "cookies + localStorage only; IndexedDB disabled",
                "note": "OK" if logged_in else "Facebook session chưa hợp lệ hoặc đã hết hạn.",
            }

    async def _collect_visible_chats(self, page: Page, out: dict[str, dict[str, str]]) -> int:
        anchors = page.locator(CHAT_SELECTOR)
        count = min(await anchors.count(), 300)
        added = 0
        for i in range(count):
            a = anchors.nth(i)
            try:
                href = await a.get_attribute("href")
                text = (await a.inner_text()).strip()
            except Exception:
                continue
            if not href:
                continue
            full = _normalize_facebook_chat_url(href)
            if not full or full in out:
                continue
            p = urlparse(full)
            lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
            thread_id = p.path.rstrip("/").rsplit("/", 1)[-1]
            name = lines[0] if lines else thread_id
            preview = " | ".join(lines[1:4])[:300]
            out[full] = {"name": name[:200], "url": full, "preview": preview}
            added += 1
        return added

    async def _scroll_conversation_list(self, page: Page) -> None:
        # Facebook virtualizes the left conversation list. Scroll the nearest
        # scrollable ancestor of a conversation link, not the whole page, when possible.
        try:
            await page.evaluate(
                """() => {
                    const a = document.querySelector('a[href*="/messages/t/"],a[href*="/messages/e2ee/t/"]');
                    let el = a;
                    while (el) {
                        const s = getComputedStyle(el);
                        if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
                            el.scrollTop = Math.min(el.scrollTop + Math.max(700, el.clientHeight * 0.9), el.scrollHeight);
                            return true;
                        }
                        el = el.parentElement;
                    }
                    window.scrollBy(0, 900);
                    return false;
                }"""
            )
        except Exception:
            try:
                await page.mouse.wheel(0, 900)
            except Exception:
                pass
        await page.wait_for_timeout(650)

    async def list_chats(self, limit: int = 20) -> list[dict[str, str]]:
        limit = max(1, min(int(limit), 100))
        async with self._lock:
            page = await self._goto_unlocked(MESSENGER_URL)
            await self._require_login(page)
            await page.wait_for_timeout(1200)

            found: dict[str, dict[str, str]] = {}
            stagnant = 0
            for _ in range(24):
                before = len(found)
                await self._collect_visible_chats(page, found)
                if len(found) >= limit:
                    break
                if len(found) == before:
                    stagnant += 1
                else:
                    stagnant = 0
                if stagnant >= 4:
                    break
                await self._scroll_conversation_list(page)

            self._touch_unlocked()
            return list(found.values())[:limit]

    async def _resolve_chat(self, chat: str) -> tuple[str, str]:
        chat = chat.strip()
        if chat.startswith("http://") or chat.startswith("https://"):
            normalized = _normalize_facebook_chat_url(chat)
            if not normalized:
                raise ValueError(
                    "URL chat phải là Facebook Messages (/messages/t/... hoặc /messages/e2ee/t/...)."
                )
            thread_id = urlparse(normalized).path.rstrip("/").rsplit("/", 1)[-1]
            return normalized, thread_id

        chats = await self.list_chats(limit=100)
        q = chat.casefold()
        exact = [c for c in chats if c["name"].casefold() == q]
        if len(exact) == 1:
            return exact[0]["url"], exact[0]["name"]
        partial = [c for c in chats if q in c["name"].casefold()]
        if len(partial) == 1:
            return partial[0]["url"], partial[0]["name"]
        if not partial:
            raise ValueError(f"Không tìm thấy cuộc trò chuyện: {chat!r}. Hãy gọi messenger_list_chats trước.")
        names = ", ".join(c["name"] for c in partial[:8])
        raise ValueError(f"Tên chat chưa đủ rõ. Có nhiều kết quả: {names}")

    async def read_chat(self, chat: str, limit: int = 30) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        url, resolved_name = await self._resolve_chat(chat)
        async with self._lock:
            page = await self._goto_unlocked(url)
            await self._require_login(page)
            await page.wait_for_timeout(1400)

            selectors = [
                '[role="main"] [role="row"]',
                '[role="main"] [data-scope="messages_table"] [role="row"]',
                '[role="main"] div[dir="auto"]',
            ]
            raw: list[str] = []
            for sel in selectors:
                loc = page.locator(sel)
                count = await loc.count()
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

            self._touch_unlocked()
            return {
                "chat": resolved_name,
                "url": page.url,
                "messages": cleaned[-limit:],
                "site": "Facebook Web Messages",
                "warning": "Facebook Web không có DOM API ổn định; kết quả được trích từ giao diện.",
            }

    async def send_message(self, chat: str, message: str) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise ValueError("message không được để trống")
        if len(message) > 4000:
            raise ValueError("message quá dài (>4000 ký tự)")
        url, resolved_name = await self._resolve_chat(chat)
        async with self._lock:
            page = await self._goto_unlocked(url)
            await self._require_login(page)
            await page.wait_for_timeout(1000)

            composer = page.locator('[role="textbox"][contenteditable="true"]')
            count = await composer.count()
            if count == 0:
                composer = page.locator('div[contenteditable="true"]')
                count = await composer.count()
            if count == 0:
                raise RuntimeError(
                    "Không tìm thấy ô soạn tin trên Facebook Web. Giao diện có thể đã đổi hoặc tài khoản cần checkpoint."
                )

            box = composer.last
            await box.click()
            try:
                await box.fill(message)
            except Exception:
                await page.keyboard.type(message)
            await box.press("Enter")
            await page.wait_for_timeout(700)
            self._touch_unlocked()
            return {"sent": True, "chat": resolved_name, "text": message, "url": page.url, "site": "Facebook Web Messages"}


messenger = MessengerBrowser()
