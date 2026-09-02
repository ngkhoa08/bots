from __future__ import annotations

import base64
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "state" / "messenger_profile"
OUT_JSON = ROOT / "runtime_state.json"
OUT_B64 = ROOT / "render_session.b64"
PROFILE.mkdir(parents=True, exist_ok=True)

print("\n=== Messenger session exporter for Render ===")
print("Chromium sẽ mở trang Messenger thật.")
print("Bạn tự đăng nhập trên trang Facebook/Messenger. Script KHÔNG đọc mật khẩu.")
print("Sau khi thấy danh sách chat, quay lại terminal và nhấn Enter.\n")

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=False,
        viewport={"width": 1100, "height": 760},
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://www.messenger.com/", wait_until="domcontentloaded", timeout=60_000)
    input("Nhấn Enter sau khi đăng nhập Messenger thành công... ")
    try:
        state = context.storage_state(indexed_db=True)
    except TypeError:
        state = context.storage_state()
    OUT_JSON.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    encoded = base64.b64encode(OUT_JSON.read_bytes()).decode("ascii")
    OUT_B64.write_text(encoded, encoding="ascii")
    context.close()

print(f"\nĐã tạo: {OUT_B64.name}")
print("Đây là dữ liệu ĐĂNG NHẬP NHẠY CẢM. Không commit lên GitHub, không gửi cho người khác.")
print("Copy toàn bộ nội dung file này vào Render Environment variable MESSENGER_STORAGE_STATE_B64.")
