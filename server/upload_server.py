"""手机上传用的本地 HTTP 服务。

职责边界（硬性约束第 7 条：这个服务只负责收图）：
- 只做两件事：给手机一个上传页面、把收到的图片交出去；
- 不 import app 里任何模块，也不碰数据库、不调识别接口；
- 收下之后的落盘、校验、入库全部由注入的 `on_receive` 回调（来自 services）完成。

调用方（app/services/upload_service.py）负责把 `on_receive` 接上。
这样可以避免 services → server → services 的循环 import。

关于 Key：页面是写死的静态 HTML，不含任何环境变量，手机端拿不到 API Key。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

# 手机随手拍一张 3~8MB 很常见，给到 15MB；再大就让用户在相册里裁一下。
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

# 分块读，避免有人传个 2GB 的文件把内存撑爆
_CHUNK_SIZE = 256 * 1024

# 这些 content-type 说明浏览器自己也说不准是什么，交给 Pillow 真读一遍再定
_PASS_THROUGH_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream", "application/unknown"})

UPLOAD_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>日语错题本 · 拍照上传</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; padding: 24px 18px 40px;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    color: #222; background: #f6f7f9;
  }
  h1 { font-size: 19px; margin: 0 0 6px; }
  .sub { font-size: 13px; color: #777; margin: 0 0 22px; line-height: 1.6; }
  .card { background: #fff; border-radius: 14px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  label.picker {
    display: block; text-align: center; padding: 34px 12px;
    border: 2px dashed #c8ccd4; border-radius: 12px; color: #4a5568; font-size: 15px;
  }
  label.picker:active { background: #f0f3f8; }
  input[type=file] { display: none; }
  .hint { font-size: 12px; color: #8a8f99; text-align: center; margin-top: 12px; line-height: 1.6; }
  #preview { width: 100%; border-radius: 10px; margin-top: 16px; display: none; }
  #status {
    margin-top: 16px; padding: 12px 14px; border-radius: 10px; font-size: 14px;
    display: none; line-height: 1.5; word-break: break-all;
  }
  .ok { background: #eaf7ee; color: #1f6b3a; border: 1px solid #a1d8b2; }
  .err { background: #fdecec; color: #a12622; border: 1px solid #e0a1a1; }
  .busy { background: #eef3fb; color: #1f4e79; border: 1px solid #a8c3e8; }
  label.picker.alt {
    border-style: solid; border-color: #d3d8e0; margin-top: 10px;
    padding: 18px 12px; font-size: 14px;
  }
  #fileinfo {
    font-size: 12px; color: #6b7280; margin-top: 10px;
    display: none; word-break: break-all; line-height: 1.6;
  }
  button#retry {
    display: none; width: 100%; margin-top: 10px; padding: 13px;
    border: 0; border-radius: 10px; background: #2f6f9f; color: #fff; font-size: 15px;
  }
  button#retry:active { opacity: .8; }
</style>
</head>
<body>
  <h1>日语错题本 · 拍照上传</h1>
  <p class="sub">拍一张生词卡或语法笔记，传完回到电脑上继续：电脑那边会自动收到，你再核对文字。</p>
  <div class="card">
    <label class="picker" for="camera">点这里拍照</label>
    <input id="camera" type="file" accept="image/*" capture="environment">
    <label class="picker alt" for="album">或者从相册里选一张</label>
    <input id="album" type="file" accept="image/*">
    <div class="hint">支持 JPG / PNG 等常见图片，单张不超过 15MB<br>iPhone 的 HEIC 请先在相册里截个图再传</div>
    <div id="fileinfo"></div>
    <div id="status"></div>
    <img id="preview" alt="预览">
    <button id="retry">重新上传这张</button>
  </div>
<script>
// 整个脚本塞进 IIFE：顶层 var 会和 window 上的同名属性（window.status 等）
// 撞车，写进去就变成字符串，后面 .style 直接 undefined。
(function () {
  var statusBox = document.getElementById('status');
  var preview = document.getElementById('preview');
  var fileinfo = document.getElementById('fileinfo');
  var retry = document.getElementById('retry');
  var lastFile = null;
  var uploading = false;

  function show(kind, text) {
    statusBox.className = kind;
    statusBox.textContent = text;
    statusBox.style.display = 'block';
  }

  // 打点：手机上什么都不显示时，电脑端 data/logs/upload.log 里还能看到它走到哪一步
  function ping(text) {
    try {
      var img = new Image(1, 1);
      img.src = '/ping?e=' + encodeURIComponent(text) + '&t=' + (new Date()).getTime();
    } catch (e) { /* 打点失败不影响上传 */ }
  }

  window.onerror = function (msg, src, line, col) {
    uploading = false;
    ping('js-error ' + msg + ' @' + line + ':' + col);
    show('err', '页面脚本出错：' + msg + '。换成 Chrome 打开本页再试。');
    retry.style.display = 'block';
    return false;
  };

  function human(bytes) {
    if (bytes >= 1048576) { return (bytes / 1048576).toFixed(1) + ' MB'; }
    if (bytes >= 1024) { return Math.round(bytes / 1024) + ' KB'; }
    return bytes + ' B';
  }

  function pick(input) {
    var file = input.files && input.files[0];
    if (!file) {
      ping('pick-empty');
      show('err', '没读到照片。请刷新页面重试；如果点了拍照又退回来还是这样，换 Chrome 或从相册里选一张。');
      return;
    }
    ping('pick ' + human(file.size) + ' ' + (file.type || 'unknown'));
    lastFile = file;
    retry.style.display = 'none';
    fileinfo.textContent = '已选：' + (file.name || 'photo') + '　'
      + human(file.size) + '　' + (file.type || '类型未知');
    fileinfo.style.display = 'block';
    try {
      preview.src = URL.createObjectURL(file);
      preview.style.display = 'block';
    } catch (e) { /* 预览不出来也照样上传 */ }
    upload(file);
  }

  function fail(text) {
    uploading = false;
    show('err', text);
    retry.style.display = 'block';
  }

  function succeed(size) {
    uploading = false;
    show('ok', '已收到（' + human(size) + '），回电脑上看吧。');
    retry.style.display = 'none';
  }

  function upload(file) {
    if (uploading) { return; }
    uploading = true;
    retry.style.display = 'none';
    show('busy', '正在上传…');
    ping('upload-start ' + human(file.size));

    var form = new FormData();
    form.append('file', file, file.name || 'photo.jpg');

    // 有些安卓 WebView（微信/QQ 内置浏览器）的 XMLHttpRequest 是残的，
    // 建对象能过、碰 upload 就抛异常。这里兜住，抛了立刻换 fetch 再试一次。
    try {
      xhrUpload(file, form);
    } catch (err) {
      ping('xhr-broken ' + ((err && err.message) ? err.message : err));
      fetchUpload(file, form);
    }
  }

  function xhrUpload(file, form) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload', true);
    xhr.timeout = 60000;
    if (xhr.upload) {
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && e.total > 0) {
          show('busy', '正在上传… ' + Math.round(e.loaded / e.total * 100) + '%');
        }
      };
    }
    xhr.onload = function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      ping('upload-done ' + xhr.status);
      if (xhr.status === 200 && data && data.ok) {
        succeed(file.size);
      } else {
        fail((data && data.message) ? data.message : ('服务器返回 ' + xhr.status));
      }
    };
    xhr.onerror = function () {
      ping('upload-neterror');
      fail('连不上电脑（网络错误）。确认手机热点还开着、电脑上的上传服务还开着，然后点「重新上传这张」。');
    };
    xhr.ontimeout = function () {
      ping('upload-timeout');
      fail('上传超时（60 秒没传完）。图片可能太大，试试先在相册里裁小一点再传。');
    };
    xhr.onabort = function () {
      ping('upload-abort');
      fail('上传被中断了，点「重新上传这张」。');
    };
    xhr.send(form);
    ping('upload-sent');
  }

  function fetchUpload(file, form) {
    if (!window.fetch) {
      fail('这台手机的浏览器太旧，没法上传。请用 Chrome 打开本页，或把照片用微信发到电脑。');
      return;
    }
    ping('fetch-fallback');
    fetch('/upload', { method: 'POST', body: form })
      .then(function (resp) {
        return resp.text().then(function (text) { return { status: resp.status, text: text }; });
      })
      .then(function (res) {
        var data = null;
        try { data = JSON.parse(res.text); } catch (e) { data = null; }
        ping('upload-done ' + res.status);
        if (res.status === 200 && data && data.ok) {
          succeed(file.size);
        } else {
          fail((data && data.message) ? data.message : ('服务器返回 ' + res.status));
        }
      })
      .catch(function (err) {
        ping('upload-fetchfail ' + ((err && err.message) ? err.message : err));
        fail('连不上电脑：' + ((err && err.message) ? err.message : err));
      });
  }

  document.getElementById('camera').addEventListener('change', function () { pick(this); });
  document.getElementById('album').addEventListener('change', function () { pick(this); });
  retry.addEventListener('click', function () {
    if (lastFile) { upload(lastFile); } else { show('err', '还没有选过照片。'); }
  });
  ping('page-load');
})();
</script>
</body>
</html>
"""


class UploadServerError(RuntimeError):
    """上传服务起不来（端口被占、绑定失败）。带 user_message 供界面直接显示。"""

    def __init__(self, user_message: str, detail: str = "") -> None:
        self.user_message = user_message
        self.detail = detail
        super().__init__(detail or user_message)


def _acceptable_type(content_type: str) -> bool:
    """是否允许交给 Pillow 再验一次。

    手机浏览器五花八门：安卓相机常常给 `application/octet-stream`，
    有的给空字符串。这类一律放行，让 Pillow 真正读一遍再决定，
    只有明确是别的类型（application/pdf、text/plain…）才当场拒掉。
    """
    ctype = (content_type or "").lower().split(";")[0].strip()
    if ctype.startswith("image/"):
        return True
    return ctype in _PASS_THROUGH_TYPES


def create_app(
    *,
    inbox: Any,
    on_receive: Callable[[bytes, str, str], Any],
    max_bytes: int = MAX_UPLOAD_BYTES,
    on_event: Callable[[str], None] | None = None,
) -> FastAPI:
    """组装 FastAPI 应用。

    inbox：收到图片后放进去的容器，桌面端轮询它；
    on_receive：把 (图片字节, 原始文件名, content-type) 变成一条接收记录，
                由 services 层提供；抛异常时其 user_message 会转给手机页面。
    on_event：一行日志的回调（由 services 写进 data/logs/upload.log）。
              "手机明明点了上传、电脑却没反应"只能靠它查。
    """
    app = FastAPI(title="jp-notebook upload", docs_url=None, redoc_url=None, openapi_url=None)

    def _log(text: str) -> None:
        if on_event is None:
            return
        try:
            on_event(text)
        except Exception:  # noqa: BLE001 - 日志写不进去也不能影响上传
            pass

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        _log("GET / 打开了上传页")
        return HTMLResponse(UPLOAD_PAGE)

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/ping")
    def ping(request: Request) -> Response:
        """手机页面打点：走到哪一步都记一行，排查"点了没反应"全靠它。"""
        _log(f"GET /ping {request.url.query[:200]}")
        return Response(status_code=204)

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)) -> JSONResponse:
        content_type = (file.content_type or "").lower()
        filename = file.filename or "photo.jpg"
        if not _acceptable_type(content_type):
            _log(f"POST /upload name={filename!r} type={content_type!r} -> 415 不是图片类型")
            return JSONResponse(
                {
                    "ok": False,
                    "message": "只能上传图片（JPG / PNG 等）。PDF、文档请先截成图片。",
                },
                status_code=415,
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                limit_mb = max(1, max_bytes // (1024 * 1024))
                _log(f"POST /upload name={filename!r} type={content_type!r} -> 413 超过 {limit_mb}MB")
                return JSONResponse(
                    {
                        "ok": False,
                        "message": f"图片太大了（上限 {limit_mb}MB），请在相册里裁一下或截图后再传。",
                    },
                    status_code=413,
                )
            chunks.append(chunk)

        try:
            received = on_receive(b"".join(chunks), filename, content_type)
        except Exception as exc:  # noqa: BLE001 - 任何失败都要变成手机能读懂的话
            message = getattr(exc, "user_message", "") or "这张图片没能存下来，请重试或换一张。"
            _log(f"POST /upload name={filename!r} type={content_type!r} bytes={total} -> 400 {message}")
            return JSONResponse({"ok": False, "message": message}, status_code=400)

        inbox.put(received)
        _log(
            f"POST /upload name={filename!r} type={content_type!r} bytes={total} -> 200 "
            f"saved {getattr(received, 'relative_path', '')}"
        )
        return JSONResponse(
            {
                "ok": True,
                "message": "已收到",
                "file": getattr(received, "relative_path", ""),
            }
        )

    return app


class UploadServer:
    """在后台线程里跑 uvicorn，桌面端只需要 start()/stop()。

    不用 multiprocessing，也不开第二个进程：图片就地交给同一个 services，
    SQLite 与配置都只有一份，省掉一切跨进程同步的麻烦。
    """

    def __init__(
        self,
        *,
        inbox: Any,
        on_receive: Callable[[bytes, str, str], Any],
        host: str = "0.0.0.0",
        port: int = 8765,
        max_bytes: int = MAX_UPLOAD_BYTES,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._inbox = inbox
        self._on_receive = on_receive
        self._host = host
        self._port = int(port)
        self._max_bytes = max_bytes
        self._on_event = on_event
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- 状态
    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        """实际监听的端口；传 0 启动时，这里会是系统分配的端口。"""
        return self._port

    @property
    def running(self) -> bool:
        return bool(
            self._server is not None
            and self._server.started
            and self._thread is not None
            and self._thread.is_alive()
        )

    # ------------------------------------------------------------- 启停
    def start(self, timeout: float = 10.0) -> None:
        if self.running:
            return

        _quiet_uvicorn()
        app = create_app(
            inbox=self._inbox,
            on_receive=self._on_receive,
            max_bytes=self._max_bytes,
            on_event=self._on_event,
        )
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            log_config=None,  # 别让 uvicorn 重置 logging（否则静音会失效）
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        # uvicorn 默认会往主线程装信号处理器，在子线程里装会抛 ValueError
        try:
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        except Exception:  # noqa: BLE001 - 不同版本 uvicorn 的写法不一样
            pass

        thread = threading.Thread(target=server.run, name="upload-server", daemon=True)
        thread.start()

        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if server.started:
                break
            if not thread.is_alive():
                break
            time.sleep(0.05)

        if not server.started:
            server.should_exit = True
            thread.join(timeout=3.0)
            raise UploadServerError(
                f"启动上传服务失败：端口 {self._port} 可能被别的程序占用，或系统不让监听。"
                "可以换一个端口，或先关掉占用它的程序。",
                detail=f"host={self._host} port={self._port} thread_alive={thread.is_alive()}",
            )

        self._server = server
        self._thread = thread
        self._port = _bound_port(server) or self._port

    def stop(self, timeout: float = 10.0) -> None:
        server, thread = self._server, self._thread
        if server is None:
            return
        server.should_exit = True
        if thread is not None:
            thread.join(timeout)
        self._server = None
        self._thread = None


def _quiet_uvicorn() -> None:
    """别让 uvicorn 自己往终端打日志。

    uvicorn 默认把启动/绑定失败的信息直接写 stderr，桌面程序里没人看终端；
    真正的错误由 UploadServerError 带着人话返回给界面，所以这里统一静音。
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False


def _bound_port(server: uvicorn.Server) -> int:
    """端口传 0 时从已绑定的 socket 上读回真实端口。"""
    for server_obj in getattr(server, "servers", []) or []:
        for sock in getattr(server_obj, "sockets", []) or []:
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError, TypeError):
                continue
    return 0


__all__ = [
    "MAX_UPLOAD_BYTES",
    "UPLOAD_PAGE",
    "UploadServer",
    "UploadServerError",
    "create_app",
]