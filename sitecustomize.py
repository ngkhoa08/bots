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
        derived = base64.urlsafe_b64encode(hashlib.sha256(access_key.encode("utf-8")).digest())
        decrypted = Fernet(derived).decrypt(encrypted_path.read_bytes().strip())
        state = json.loads(decrypted.decode("utf-8"))
        if isinstance(state, dict) and isinstance(state.get("cookies"), list):
            os.environ["MESSENGER_STORAGE_STATE_B64"] = base64.b64encode(decrypted).decode("ascii")
            os.environ.setdefault("FACEBOOK_MESSAGES_URL", "https://www.facebook.com/messages/")
            print("[bootstrap] encrypted default Facebook session loaded")
except Exception as exc:
    print(f"[bootstrap] default Facebook session unavailable: {type(exc).__name__}")
