"""阶段 5 验收脚本（服务层）：真起一个 HTTP 服务，用 requests 打它。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_upload.py

覆盖：收件箱、图片落盘与文件名清洗、坏图/空图、接口 200/400/413/415、
手机页面不含 API Key、停止后端口释放、端口被占用时有明确错误、可重启。
全程只用 127.0.0.1，不碰外网，也不需要手机。
"""

from __future__ import annotations

import io
import shutil
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from PIL import Image

from app import config
from app.services import upload_service
from app.services.upload_service import INBOX, UploadFailed
from server.queue import Inbox
from server.upload_server import UploadServer, UploadServerError

WORK_DIR = config.DATA_DIR / "_verify_upload"
UPLOAD_ROOT = WORK_DIR / "uploads"

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


def jpeg_bytes(size: tuple[int, int] = (120, 90), color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def port_closed(port: int, timeout: float = 1.0) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    config.DATA_DIR = WORK_DIR
    config.UPLOAD_DIR = UPLOAD_ROOT
    config.DB_PATH = WORK_DIR / "verify.db"
    config.LOG_DIR = WORK_DIR / "logs"

    # ---------------------------------------------------------------- 1
    print("\n== 1. 收件箱（跨线程用） ==")
    box: Inbox[str] = Inbox(max_items=3)
    check("初始为空", len(box) == 0 and box.latest() is None)
    check("放入返回版本号 1", box.put("a") == 1)
    box.put("b")
    box.put("c")
    check("满了之后保留最新的", box.snapshot() == ["a", "b", "c"], str(box.snapshot()))
    box.put("d")
    check("超过上限丢最旧的", box.snapshot() == ["b", "c", "d"], str(box.snapshot()))
    check("版本号只增不减", box.version() == 4, str(box.version()))
    check("latest 是最后放进去的", box.latest() == "d", str(box.latest()))
    snapshot = box.snapshot()
    snapshot.clear()
    check("snapshot 是拷贝，改了不影响原队列", len(box) == 3, str(len(box)))
    box.clear()
    check("clear 后为空但版本号还在", len(box) == 0 and box.version() == 4)

    # ---------------------------------------------------------------- 2
    print("\n== 2. 图片落盘 ==")
    INBOX.clear()
    data = jpeg_bytes()
    received = upload_service.save_received(data, "../../手机相册/IMG_0001.jpg", "image/jpeg")
    check("返回的是相对路径", received.relative_path.count("/") == 1, received.relative_path)
    check("按日期分了目录", received.relative_path.split("/")[0].count("-") == 2,
          received.relative_path)
    check("落在 uploads 下且真的存在", received.absolute_path.is_file())
    check("后缀由图片格式决定（jpg）", received.relative_path.endswith(".jpg"),
          received.relative_path)
    check("大小与传进去的一致", received.size == len(data), f"{received.size} vs {len(data)}")
    check("文件名里的路径被剥掉", received.original_name == "IMG_0001.jpg",
          received.original_name)
    check("收到时间有秒", len(received.received_at) == 19, received.received_at)
    check("clock_text 是时分秒", received.clock_text == received.received_at[11:19],
          received.clock_text)
    check("size_text 是人能读的", received.size_text[-2:] in ("KB", "MB", " B"),
          received.size_text)

    odd = upload_service.save_received(jpeg_bytes(), "", "image/jpeg")
    check("没有文件名时给个默认名", odd.original_name == "photo.jpg", odd.original_name)
    check("临时文件没有留在 _incoming 里",
          not list((UPLOAD_ROOT / "_incoming").glob("*.upload")),
          str(list((UPLOAD_ROOT / "_incoming").glob("*"))))

    print("\n== 3. 坏输入 ==")
    for label, call in (
        ("空文件", lambda: upload_service.save_received(b"", "a.jpg", "image/jpeg")),
        ("不是图片", lambda: upload_service.save_received(b"not an image", "a.jpg", "image/jpeg")),
        (
            "白名单外的格式",
            lambda: upload_service.save_received(_ico_bytes(), "a.ico", "image/x-icon"),
        ),
    ):
        try:
            call()
        except UploadFailed as exc:
            check(f"{label} 抛 UploadFailed 且有人话", bool(exc.user_message), exc.user_message)
        except Exception as exc:  # noqa: BLE001
            check(f"{label} 抛 UploadFailed", False, f"实际是 {type(exc).__name__}: {exc}")
        else:
            check(f"{label} 抛 UploadFailed", False, "没有抛异常")

    # ---------------------------------------------------------------- 4
    print("\n== 4. 启动服务 ==")
    port = free_port()
    server = upload_service.start_server(port)
    base = f"http://127.0.0.1:{port}"
    check("服务在跑", upload_service.is_running())
    check("端口是传进去的那个", upload_service.port() == port, str(upload_service.port()))
    check("地址是 http://IP:端口", upload_service.base_url().startswith("http://")
          and upload_service.base_url().endswith(f":{port}"), upload_service.base_url())
    check("本机 IP 不是 0.0.0.0", upload_service.lan_ip() not in ("", "0.0.0.0"),
          upload_service.lan_ip())
    check("重复 start 返回同一个实例", upload_service.start_server(port) is server)

    print("\n== 5. 手机页面 ==")
    response = requests.get(base + "/", timeout=10)
    html = response.text
    check("页面 200", response.status_code == 200, str(response.status_code))
    check("有拍照上传的 input", 'type="file"' in html and "image/*" in html)
    check("调用了后置摄像头", 'capture="environment"' in html)
    check("还有一条不带 capture 的相册入口", html.count('type="file"') == 2, str(html.count('type="file"')))
    check("上传失败可以点按钮重试", 'id="retry"' in html)
    check("用 XHR 而不是 fetch（能看进度和超时）", "XMLHttpRequest" in html and "xhr.upload.onprogress" in html)
    check("页面会往 /ping 打点", "/ping?e=" in html)
    check("XHR 抛异常时自动退回 fetch", "function fetchUpload" in html and "xhr-broken" in html)
    check("脚本错误的原文也进日志", "'js-error ' + msg" in html)
    check("提示条在预览图上面（不用滚动就能看见）",
          html.index('id="status"') < html.index('id="preview"'))
    check("读到空文件时也有提示", "没读到照片" in html)
    check("页面里没有接口地址", config.get_settings().base_url not in html)
    check("页面里没有 API Key 变量名", "OPENAI_API_KEY" not in html)
    api_key = config.get_settings().api_key
    check("页面里没有真实 Key", (api_key is None) or (api_key not in html),
          "HTML 出现了 Key" if api_key else "")
    check("页面不引用外部脚本或样式",
          "<script src" not in html and 'rel="stylesheet"' not in html)

    health = requests.get(base + "/health", timeout=10)
    check("健康检查返回 ok", health.status_code == 200 and health.json() == {"ok": True},
          health.text[:80])

    pong = requests.get(base + "/ping", params={"e": "pick 1.2 MB image/jpeg"}, timeout=10)
    check("/ping 返回 204", pong.status_code == 204, str(pong.status_code))
    check("/ping 没有响应体", pong.text == "", pong.text[:40])

    # ---------------------------------------------------------------- 6
    print("\n== 6. 正常上传 ==")
    before = len(INBOX)
    files = {"file": ("IMG_1234.jpg", jpeg_bytes((300, 200)), "image/jpeg")}
    uploaded = requests.post(base + "/upload", files=files, timeout=15)
    body = uploaded.json()
    check("上传返回 200", uploaded.status_code == 200, str(uploaded.status_code) + uploaded.text[:120])
    check("返回 ok=true", body.get("ok") is True, str(body))
    check("返回里有相对路径", "/" in str(body.get("file", "")), str(body.get("file")))
    check("收件箱多了一张", len(INBOX) == before + 1, f"{before} -> {len(INBOX)}")
    latest = INBOX.latest()
    check("收件箱里是刚传那张", latest is not None and latest.size > 0)
    check("文件真的落盘了", latest is not None and latest.absolute_path.is_file())

    print("\n== 6b. 手机只肯说 octet-stream / 空类型时也要收 ==")
    before = len(INBOX)
    octet = requests.post(
        base + "/upload",
        files={"file": ("IMG_7777.jpg", jpeg_bytes((240, 160)), "application/octet-stream")},
        timeout=15,
    )
    check("octet-stream 的真图能收（200）", octet.status_code == 200,
          str(octet.status_code) + octet.text[:120])
    check("octet-stream 上传后进了收件箱", len(INBOX) == before + 1, f"{before} -> {len(INBOX)}")

    no_type = requests.post(
        base + "/upload",
        files={"file": ("IMG_8888.jpg", jpeg_bytes((240, 160)), "")},
        timeout=15,
    )
    check("content-type 为空也能收（200）", no_type.status_code == 200,
          str(no_type.status_code) + no_type.text[:120])

    octet_garbage = requests.post(
        base + "/upload",
        files={"file": ("x.jpg", b"still not an image", "application/octet-stream")},
        timeout=10,
    )
    check("octet-stream 的垃圾内容仍被拒（400）", octet_garbage.status_code == 400,
          str(octet_garbage.status_code))

    # ---------------------------------------------------------------- 7
    print("\n== 7. 各种失败 ==")
    before_fail = len(INBOX)
    pdf = requests.post(
        base + "/upload",
        files={"file": ("scan.pdf", b"%PDF-1.4 fake", "application/pdf")},
        timeout=10,
    )
    check("PDF 被拒（415）", pdf.status_code == 415, str(pdf.status_code))
    check("PDF 的提示提到只能传图片", "图片" in pdf.json().get("message", ""),
          pdf.json().get("message", ""))

    weird = requests.post(
        base + "/upload",
        files={"file": ("x.jpg", b"this is not an image", "image/jpeg")},
        timeout=10,
    )
    check("假 jpg 被拒（400）", weird.status_code == 400, str(weird.status_code))
    check("假 jpg 的提示是人话", bool(weird.json().get("message")), weird.json().get("message", ""))
    check("失败时没有往收件箱塞东西", len(INBOX) == before_fail, f"{before_fail} -> {len(INBOX)}")

    empty = requests.post(base + "/upload", files={"file": ("e.jpg", b"", "image/jpeg")}, timeout=10)
    check("空文件被拒（400）", empty.status_code == 400, str(empty.status_code))

    print("\n== 7b. 上传流水账（手机端出问题只能靠它查） ==")
    log_file = upload_service.log_path()
    check("日志文件写出来了", log_file.is_file(), str(log_file))
    log_text = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
    check("记下了打开的首页", "GET / 打开了上传页" in log_text)
    check("记下了页面打点", "GET /ping e=pick" in log_text, log_text[-300:])
    check("记下了成功的那次上传", "-> 200 saved" in log_text, log_text[-200:])
    check("成功日志里有文件名", "IMG_1234.jpg" in log_text)
    check("成功日志里有 content-type", "type='image/jpeg'" in log_text, log_text[-300:])
    check("成功日志里有字节数", "bytes=" in log_text)
    check("记下了 415", "-> 415" in log_text and "scan.pdf" in log_text, log_text[-300:])
    check("记下了 400", "-> 400" in log_text and "x.jpg" in log_text, log_text[-300:])
    check("记下了启动", "上传服务已启动" in log_text, log_text[:300])
    check("每行都有时间戳", all(line[:2] == "20" for line in log_text.splitlines() if line))

    print("\n== 8. 超过大小上限 ==")
    tiny_port = free_port()
    tiny = UploadServer(
        inbox=Inbox(),
        on_receive=upload_service.save_received,
        host="127.0.0.1",
        port=tiny_port,
        max_bytes=2048,
    )
    tiny.start()
    try:
        big = requests.post(
            f"http://127.0.0.1:{tiny_port}/upload",
            files={"file": ("big.jpg", jpeg_bytes((900, 900)), "image/jpeg")},
            timeout=15,
        )
        check("大图被拒（413）", big.status_code == 413, str(big.status_code))
        message = big.json().get("message", "")
        check("大图的提示说了上限", "MB" in message and "上限" in message, message)
    finally:
        tiny.stop()

    # ---------------------------------------------------------------- 9
    print("\n== 9. 端口占用与重启 ==")
    occupied = UploadServer(
        inbox=Inbox(),
        on_receive=upload_service.save_received,
        host="0.0.0.0",
        port=port,
    )
    try:
        occupied.start()
    except UploadServerError as exc:
        check("端口被占时给出人话", "占用" in exc.user_message or "失败" in exc.user_message,
              exc.user_message)
    else:
        occupied.stop()
        check("端口被占时给出人话", False, "居然起来了")

    upload_service.stop_server()
    check("停止后状态是不在跑", not upload_service.is_running())
    check("停止后端口真的放开了", port_closed(port), f"127.0.0.1:{port} 还能连")

    upload_service.start_server(port)
    check("同一端口能重启", upload_service.is_running())
    again = requests.get(f"http://127.0.0.1:{port}/health", timeout=10)
    check("重启后还能正常响应", again.status_code == 200, str(again.status_code))
    upload_service.stop_server()

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed == 0:
        print("手机上传服务验收全部通过。")
    return 0 if _failed == 0 else 1


def _ico_bytes() -> bytes:
    """Pillow 能打开、但不在白名单里的格式（验证"格式不支持"这条分支）。"""
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 10, 10)).save(buffer, format="ICO")
    return buffer.getvalue()


if __name__ == "__main__":
    code = main()
    upload_service.stop_server()
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    raise SystemExit(code)