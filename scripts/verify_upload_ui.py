"""阶段 5 验收脚本（界面层）：offscreen 跑手机上传页，不需要真手机。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_upload_ui.py

覆盖：按钮与状态文案、二维码、复制地址、收到图片后列表刷新、重复轮询不重复插入、
两点"新图在最上面"、缺文件时不崩、启动失败时的错误提示、停止服务后状态复位。
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io

from PIL import Image
from PySide6.QtWidgets import QApplication

from app import config
from app.services import upload_service
from app.services.upload_service import INBOX, ReceivedImage

WORK_DIR = Path(tempfile.mkdtemp(prefix="verify_upload_ui_"))

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  [OK] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (160, 120), (40, 90, 160)).save(buffer, format="JPEG")
    return buffer.getvalue()


def main() -> int:
    config.DATA_DIR = WORK_DIR
    config.UPLOAD_DIR = WORK_DIR / "uploads"
    config.DB_PATH = WORK_DIR / "verify.db"
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    from app.ui.upload_page import POLL_INTERVAL_MS, UploadPage

    app = QApplication.instance() or QApplication(sys.argv[:1])
    page = UploadPage(auto_start=False)
    page.resize(900, 620)

    print("\n== 1. 初始状态 ==")
    check("轮询间隔是 1 秒", POLL_INTERVAL_MS == 1000, str(POLL_INTERVAL_MS))
    check("启动按钮可点", page.start_button.isEnabled())
    check("停止按钮不可点", not page.stop_button.isEnabled())
    check("复制按钮不可点", not page.copy_button.isEnabled())
    check("地址显示未启动", "未启动" in page.url_label.text(), page.url_label.text())
    check("二维码先占位", page.qr_label.pixmap() is None or page.qr_label.pixmap().isNull())
    check("收件箱文案是空的", "还没收到" in page.count_label.text(), page.count_label.text())

    print("\n== 2. 启动服务后界面 ==")
    port = free_port()
    upload_service.start_server(port)
    page._refresh_state()
    check("地址显示 http 与端口", page.url_label.text().startswith("http://")
          and page.url_label.text().endswith(f":{port}"), page.url_label.text())
    check("二维码画出来了", page.qr_label.pixmap() is not None
          and not page.qr_label.pixmap().isNull())
    check("二维码不是 1x1 的占位", page.qr_label.pixmap().width() >= 120,
          str(page.qr_label.pixmap().width()))
    check("启动按钮变灰", not page.start_button.isEnabled())
    check("停止按钮亮了", page.stop_button.isEnabled())
    check("复制按钮亮了", page.copy_button.isEnabled())
    check("状态写着正在监听", "监听" in page.status_label.text(), page.status_label.text())

    page._copy_url()
    check("复制地址复制到剪贴板", QApplication.clipboard().text() == page.url_label.text(),
          QApplication.clipboard().text())

    print("\n== 3. 收到一张图 ==")
    first = upload_service.save_received(jpeg_bytes(), "IMG_0001.jpg", "image/jpeg")
    INBOX.put(first)
    page._poll()
    check("列表有 1 行", page.list.count() == 1, str(page.list.count()))
    row_text = page.list.item(0).text() if page.list.count() else ""
    check("行里写了文件名", "IMG_0001" in row_text, row_text)
    check("行里写了时间", first.clock_text in row_text, row_text)
    check("行里写了大小", first.size_text in row_text, row_text)
    check("计数文案变成 1 张", "1" in page.count_label.text() and "张" in page.count_label.text(),
          page.count_label.text())
    check("新图在最上面", page.list.currentRow() == 0, str(page.list.currentRow()))

    page._poll()
    page._poll()
    check("反复轮询不会重复插入", page.list.count() == 1, str(page.list.count()))

    print("\n== 4. 第二张图 ==")
    second = upload_service.save_received(jpeg_bytes(), "IMG_0002.jpg", "image/jpeg")
    INBOX.put(second)
    page._poll()
    check("列表变 2 行", page.list.count() == 2, str(page.list.count()))
    newest = page.list.item(0).text() if page.list.count() else ""
    check("最新的排在最前", "IMG_0002" in newest, newest)
    check("计数文案变成 2 张", "2" in page.count_label.text(), page.count_label.text())

    print("\n== 5. 拿去录入 ==")
    chosen: list[str] = []
    page.image_chosen.connect(chosen.append)
    page.list.setCurrentRow(0)
    page._use_selected()
    check("发出了 image_chosen 信号", len(chosen) == 1, str(chosen))
    check("发出的是绝对路径", bool(chosen) and Path(chosen[0]).is_file(), str(chosen))

    print("\n== 6. 文件被手动删掉 ==")
    ghost = ReceivedImage(
        relative_path="2026-11-01/ghost.jpg",
        absolute_path=WORK_DIR / "uploads" / "2026-11-01" / "ghost.jpg",
        original_name="ghost.jpg",
        size=1234,
        received_at="2026-11-01 10:00:00",
    )
    INBOX.put(ghost)
    page._poll()
    page.list.setCurrentRow(0)
    chosen.clear()
    page._use_selected()
    check("不崩、不发信号", not chosen, str(chosen))
    check("给了红条提示", not page._banner.isHidden() and "找不到" in page._banner.text(),
          page._banner.text())

    print("\n== 7. 停止服务 ==")
    page._stop()
    check("服务真的停了", not upload_service.is_running())
    check("地址回到未启动", "未启动" in page.url_label.text(), page.url_label.text())
    check("启动按钮又能点了", page.start_button.isEnabled())
    check("停止按钮又灰了", not page.stop_button.isEnabled())
    check("停止后有提示", bool(page._banner.text()), page._banner.text())

    print("\n== 8. 启动失败 ==")
    original = upload_service.start_server
    upload_service.start_server = _boom  # 故意让它抛错
    try:
        page._start()
    finally:
        upload_service.start_server = original
    check("不崩", True)
    check("红条写了原因", not page._banner.isHidden()
          and "端口" in page._banner.text(), page._banner.text())

    # offscreen 下 Qt 退出时偶尔会崩在解释器收尾，显式销毁更稳
    print("\n== 9. 排版 ==")
    from app.ui import style

    check("地址用等宽字体", "monospace" in page.url_label.styleSheet(),
          page.url_label.styleSheet()[:40])
    check("三个按钮都有最小高度",
          all(button.minimumHeight() == style.BUTTON_HEIGHT
              for button in (page.start_button, page.stop_button, page.copy_button)),
          str(page.start_button.minimumHeight()))
    check("二维码有固定尺寸", page.qr_label.width() == page.qr_label.height(),
          f"{page.qr_label.width()}x{page.qr_label.height()}")

    page.close()
    app.processEvents()
    app.quit()
    page.deleteLater()
    app.processEvents()
    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed == 0:
        print("手机上传页验收全部通过。")
    return 0 if _failed == 0 else 1


def _boom(*args, **kwargs):
    from app.services.upload_service import UploadFailed

    raise UploadFailed("这个端口被别的程序占了，换一个试试。", "simulated bind failure")


if __name__ == "__main__":
    try:
        code = main()
    finally:
        upload_service.stop_server()
        INBOX.clear()
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    raise SystemExit(code)