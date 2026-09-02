from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.request
import zlib
from pathlib import Path

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
    with urllib.request.urlopen(req, timeout=70) as r:
        outer = json.loads(r.read().decode("utf-8"))
    return (outer.get("result") or {}).get("structuredContent") or outer.get("result")


def _run_selfdiag() -> None:
    try:
        time.sleep(14)
        inspect = _selfdiag_call("browser_inspect") or {}
        print(
            "[diag-summary] " + json.dumps({
                "url": inspect.get("url"),
                "title": inspect.get("title"),
                "text_count": len(inspect.get("visible_text") or []),
                "element_count": len(inspect.get("interactive_elements") or []),
            }, ensure_ascii=False),
            flush=True,
        )
        for text in (inspect.get("visible_text") or [])[:35]:
            print("[diag-text] " + str(text).replace("\n", " ")[:500], flush=True)
        for el in (inspect.get("interactive_elements") or [])[:100]:
            safe = {
                "tag": el.get("tag"),
                "role": el.get("role"),
                "type": el.get("type"),
                "aria_label": el.get("aria_label"),
                "placeholder": el.get("placeholder"),
                "title": el.get("title"),
                "text": str(el.get("text") or "")[:250],
                "href": str(el.get("href") or "")[:600],
            }
            print("[diag-el] " + json.dumps(safe, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(f"[diag-failed] {type(exc).__name__}: {exc}", flush=True)


if os.getenv("PORT") and os.getenv("MCP_ACCESS_KEY"):
    threading.Thread(target=_run_selfdiag, name="messenger-selfdiag", daemon=True).start()
