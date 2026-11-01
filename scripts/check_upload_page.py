"""把手机上传页的 HTML 取出来，交给 Node 跑一遍里面的 JS。

页面脚本只在手机浏览器里执行，Python 这边的 verify_upload.py 只能看见
后端行为，看不见前端有没有抛异常。2026-11-01 那次"手机拍完照电脑收不到"
就是前端抛异常造成的（`var status` 撞上 window.status），所以单独加这一道。

用法：
    .\\venv\\Scripts\\python.exe scripts\\check_upload_page.py

没装 Node 时会打印"跳过"并返回 0 —— 它只是额外的一道保险，
不该让主流程因为缺 node 而失败。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.upload_server import UPLOAD_PAGE  # noqa: E402


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("没找到 node，跳过上传页 JS 验收（后端验收仍然有效）。")
        return 0

    script = ROOT / "scripts" / "check_upload_page_js.js"
    if not script.is_file():
        print(f"缺少 {script}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(UPLOAD_PAGE, encoding="utf-8")
        completed = subprocess.run(
            [node, str(script), str(page)],
            cwd=str(ROOT),
            text=True,
            encoding="utf-8",
            capture_output=True,
        )

    sys.stdout.write(completed.stdout)
    if completed.stderr.strip():
        sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())