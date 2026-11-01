"""手机上传的业务门面：起停服务、算本机地址、把收到的图片落盘。

分工：
- `server/upload_server.py` 只管 HTTP 收图与手机页面；
- 本模块负责"收下来的字节怎么变成一张存进 data/uploads 的图片"，
  直接复用录入那条路（`entry_service.store_image`），不另写一套存图逻辑；
- 桌面端界面只跟本模块打交道（不 import server 包），出错统一抛 `UploadFailed`。

关于依赖方向：本模块 import server 包，是为了**持有服务实例的生命周期**；
server 包反过来不 import app 任何东西，因此不会形成环。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app import config
from app.services import entry_service
from app.services.errors import ImageInvalid
from server.queue import Inbox
from server.upload_server import MAX_UPLOAD_BYTES, UploadServer, UploadServerError

__all__ = [
    "INBOX",
    "MAX_UPLOAD_BYTES",
    "ReceivedImage",
    "UploadFailed",
    "base_url",
    "is_running",
    "lan_ip",
    "port",
    "save_received",
    "start_server",
    "stop_server",
]


@dataclass(frozen=True)
class ReceivedImage:
    """手机传来的一张图。桌面端拿到它就去录入页识别。"""

    relative_path: str          # 如 2026-11-01/ab12cd34ef56.jpg
    absolute_path: Path
    size: int                   # 字节数
    original_name: str          # 手机上那张文件的名字，仅供显示
    received_at: str            # 收到的时间，%Y-%m-%d %H:%M:%S

    @property
    def size_text(self) -> str:
        if self.size >= 1024 * 1024:
            return f"{self.size / (1024 * 1024):.1f} MB"
        if self.size >= 1024:
            return f"{self.size / 1024:.0f} KB"
        return f"{self.size} B"

    @property
    def clock_text(self) -> str:
        """列表里只显示时分秒，日期已经在路径里了。"""
        return self.received_at[11:19] if len(self.received_at) >= 19 else self.received_at


class UploadFailed(RuntimeError):
    """上传链路的问题（服务起不来、图片坏了、磁盘写不进）。带 user_message 供界面显示。"""

    def __init__(self, user_message: str, detail: str = "") -> None:
        self.user_message = user_message
        self.detail = detail
        super().__init__(detail or user_message)


# 全局收件箱：服务器往里放，界面轮询它
INBOX: Inbox[ReceivedImage] = Inbox()

# 服务实例与真实端口（端口传 0 时会被系统分配，必须记下来）
_server: UploadServer | None = None
_server_port: int = 0


# --------------------------------------------------------------- 地址
def lan_ip() -> str:
    """本机在局域网里的地址。用 UDP connect 让系统挑出口网卡，不会真的发包。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()

    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def port() -> int:
    """服务实际用的端口：没启动时回落到配置里的 UPLOAD_PORT。"""
    if _server is not None and _server.running:
        return _server_port
    return config.get_settings().upload_port


def base_url() -> str:
    return f"http://{lan_ip()}:{port()}"


# --------------------------------------------------------------- 收图
def save_received(
    data: bytes,
    original_name: str,
    content_type: str = "image/jpeg",
    *,
    root: Path | None = None,
) -> ReceivedImage:
    """把手机传来的字节落成一张图片，返回接收记录（还没有入库）。

    落盘走 entry_service.store_image：Pillow 校验格式、按日期分目录、只写相对路径，
    与电脑端选图完全是同一条路。
    """
    if not data:
        raise UploadFailed("手机传上来的是个空文件，请在手机上重新拍一张。")

    name = _clean_name(original_name)
    incoming = (root or entry_service.upload_root()) / "_incoming"
    try:
        incoming.mkdir(parents=True, exist_ok=True)
        temp = incoming / f"{uuid4().hex}.upload"
        temp.write_bytes(data)
    except OSError as exc:
        raise UploadFailed(
            "保存上传的图片失败，请检查 data 目录是否可写、磁盘是否还有空间。",
            f"content_type={content_type!r} name={name!r} {exc}",
        ) from exc

    try:
        relative = entry_service.store_image(temp, root=root)
    except ImageInvalid as exc:
        raise UploadFailed(exc.user_message, exc.detail) from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass  # 临时文件删不掉不影响这次上传成功

    return ReceivedImage(
        relative_path=relative,
        absolute_path=(root or entry_service.upload_root()) / relative,
        size=len(data),
        original_name=name,
        received_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _clean_name(name: str) -> str:
    base = Path(name or "").name.strip()
    base = "".join(ch for ch in base if ch.isprintable())
    if not base:
        return "photo.jpg"
    return base[:80]


# --------------------------------------------------------------- 日志
def log_path() -> Path:
    """手机上传的流水账：data/logs/upload.log。"""
    return config.LOG_DIR / "upload.log"


def log_event(text: str) -> None:
    """写一行日志。

    手机端"点了上传但电脑没反应"时，这是唯一能看见它到底发了什么的窗口：
    文件名、content-type、字节数、最终状态码都会落在这里。
    日志写失败（磁盘满、目录被占）不能反过来把上传搞挂，所以整体吞掉。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with log_path().open("a", encoding="utf-8") as fp:
            fp.write(f"{stamp} {text}\n")
    except OSError:
        pass


# --------------------------------------------------------------- 启停
def start_server(
    port: int | None = None,
    *,
    inbox: Inbox[ReceivedImage] | None = None,
    host: str = "0.0.0.0",
) -> UploadServer:
    """启动上传服务（已在跑就直接返回现有实例）。

    端口被占用等情况一律包成 UploadFailed，界面只需要认识这一个异常。
    """
    global _server, _server_port

    if _server is not None and _server.running:
        return _server

    target = config.get_settings().upload_port if port is None else int(port)
    server = UploadServer(
        inbox=INBOX if inbox is None else inbox,
        on_receive=save_received,
        host=host,
        port=target,
        on_event=log_event,
    )
    try:
        server.start()
    except UploadServerError as exc:
        log_event(f"启动失败 host={host} port={target} {exc.detail or exc.user_message}")
        raise UploadFailed(exc.user_message, exc.detail) from exc

    _server = server
    _server_port = server.port
    log_event(f"上传服务已启动 http://{lan_ip()}:{server.port}")
    return server


def stop_server() -> None:
    global _server, _server_port
    server, _server = _server, None
    _server_port = 0
    if server is not None:
        server.stop()
        log_event("上传服务已停止")


def is_running() -> bool:
    return _server is not None and _server.running