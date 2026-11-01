"""把识别请求放到子线程里跑，避免界面卡死。

为什么必须单独一个 QThread：一次识别要 3~30 秒（网络 + 模型），直接在按钮回调里
调 `recognize()` 会让整个窗口在这段时间内点不动、也不重绘，用户会以为程序崩了。

失败原因用两个信号分开传：
- `failed(user_message, detail)`：`user_message` 是给人看的一句话，`detail` 只进日志。
  两者分离是为了界面上永远不出现 requests 的原始异常文本。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.services import entry_service
from app.services.errors import RecognitionError


class RecognitionWorker(QThread):
    """跑一次 `entry_service.recognize_image()`。"""

    succeeded = Signal(object)  # models.Entry
    failed = Signal(str, str)  # user_message, detail

    def __init__(
        self,
        image_path: str | Path,
        entry_type: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._image_path = Path(image_path)
        self._entry_type = entry_type

    def run(self) -> None:  # 在子线程执行
        try:
            entry = entry_service.recognize_image(self._image_path, self._entry_type)
        except RecognitionError as exc:
            self.failed.emit(exc.user_message, exc.detail)
        except Exception as exc:  # noqa: BLE001 - 子线程里漏出去的异常会静默吞掉整个识别
            self.failed.emit(
                "识别时出了点意外，可以重试一次，或者直接手动填写。",
                f"{type(exc).__name__}: {exc}",
            )
        else:
            self.succeeded.emit(entry)