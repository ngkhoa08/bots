from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

try:
    from cryptography.fernet import Fernet

    access_key = os.getenv("MCP_ACCESS_KEY", "").strip()
    encrypted_path = Path(__file__).resolve().with_name("default_facebook_session.enc")

    if access_key and encrypted_path.exists():
        digest = hashlib.sha256(access_key.encode("utf-8")).digest()
        fingerprint = hashlib.sha256(access_key.encode("utf-8")).hexdigest()[:12]
        derived = base64.urlsafe_b64encode(digest)
        decrypted = Fernet(derived).decrypt(encrypted_path.read_bytes().strip())
        state = json.loads(decrypted.decode("utf-8"))
        if isinstance(state, dict) and isinstance(state.get("cookies"), list):
            os.environ["MESSENGER_STORAGE_STATE_B64"] = base64.b64encode(decrypted).decode("ascii")
            os.environ.setdefault("FACEBOOK_MESSAGES_URL", "https://www.facebook.com/messages/")
            print(f"[bootstrap] encrypted default Facebook session loaded keyfp={fingerprint}")
except Exception as exc:
    fp = hashlib.sha256(os.getenv("MCP_ACCESS_KEY", "").strip().encode("utf-8")).hexdigest()[:12]
    print(f"[bootstrap] default Facebook session unavailable: {type(exc).__name__} keyfp={fp}")
