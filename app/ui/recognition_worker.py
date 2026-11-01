"""把识别放到子线程，避免真调接口那 3~10 秒把界面卡死。

这一层只负责"搬运"：把参数递进去，把结果或人话搬出来。失败不抛异常给 Qt，
只发信号——子线程里漏出去的异常在 PySide6 里会被静默吞掉，比崩溃更难查。
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
        crop: tuple[float, float, float, float] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._image_path = Path(image_path)
        self._entry_type = entry_type
        self._crop = crop

    def run(self) -> None:  # 子线程执行
        try:
            entry = entry_service.recognize_image(
                self._image_path,
                self._entry_type,
                crop=self._crop,
            )
        except RecognitionError as exc:
            self.failed.emit(exc.user_message, exc.detail)
        except Exception as exc:  # noqa: BLE001 - 子线程漏出去的异常会静默吞掉
            self.failed.emit(
                "识别时出了点问题，请稍后再试一次，或者直接在右边手填。",
                f"{type(exc).__name__}: {exc}",
            )
        else:
            self.succeeded.emit(entry)