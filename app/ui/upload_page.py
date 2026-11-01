"""手机上传页：本机地址 + 二维码 + 收到的图片列表。

手机端只负责传图，真正"这张图变成一条生词/语法记录"仍然走录入页那条流程：
本页把图交给主窗口，主窗口把它塞进录入页，用户照常识别、校对、保存。

服务不在这里写死启停时机：进页面时自动起一次（失败就红条说明原因），
也可以手动停掉——比如想换个端口，或者不想让手机连进来。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import config
from app.services import entry_service, upload_service
from app.services.upload_service import UploadFailed

_BANNER_STYLES = {
    "error": "border: 1px solid #e0a1a1; background: #fdecec; color: #a12622;",
    "ok": "border: 1px solid #a1d8b2; background: #eaf7ee; color: #1f6b3a;",
    "info": "border: 1px solid #a8c3e8; background: #eef3fb; color: #1f4e79;",
}

_QR_SIZE = 232

POLL_INTERVAL_MS = 1000


def _qr_pixmap(text: str, size: int = _QR_SIZE) -> QPixmap:
    """把地址渲染成二维码位图。不落盘，直接走内存里的 PNG。"""
    import qrcode

    qr = qrcode.QRCode(border=1, box_size=10)
    qr.add_data(text)
    qr.make(fit=True)

    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()

    pixmap = QPixmap()
    pixmap.loadFromData(data, "PNG")
    return pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class UploadPage(QWidget):
    """手机上传页。"""

    image_chosen = Signal(str)  # 选中的图片绝对路径，交给录入页
    message = Signal(str)       # 给主窗口状态栏的一句话

    def __init__(self, parent: QWidget | None = None, *, auto_start: bool = True) -> None:
        super().__init__(parent)
        self._auto_start = auto_start
        self._auto_started = False
        self._seen_version = 0
        self._last_running = False
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        self._refresh_state()

    # ------------------------------------------------------------- 构建界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel("手机上传")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        hint = QLabel(
            "手机连上同一个 Wi-Fi，扫码或手动输入地址，拍照后图片会自动出现在右边。"
            "第一次启动时 Windows 可能弹防火墙提示，选「允许」即可（只允许本地网络）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 12px; color: #666;")
        root.addWidget(hint)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        body = QHBoxLayout()
        body.setSpacing(20)
        body.addWidget(self._build_left_column(), 0)
        body.addWidget(self._build_right_column(), 1)
        root.addLayout(body, 1)

    def _build_left_column(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        panel.setFixedWidth(320)

        caption = QLabel("在手机浏览器里打开")
        caption.setStyleSheet("font-size: 12px; color: #888;")
        column.addWidget(caption)

        self.url_label = QLabel("服务未启动")
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label.setWordWrap(True)
        self.url_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        column.addWidget(self.url_label)

        self.qr_label = QLabel("服务启动后显示二维码")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setFixedSize(_QR_SIZE, _QR_SIZE)
        self.qr_label.setStyleSheet(
            "border: 1px solid #e2e2e2; border-radius: 8px; color: #999; font-size: 12px;"
        )
        column.addWidget(self.qr_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = QPushButton("启动服务")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("停止服务")
        self.stop_button.clicked.connect(self._stop)
        self.copy_button = QPushButton("复制地址")
        self.copy_button.clicked.connect(self._copy_url)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.copy_button)
        column.addLayout(buttons)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 12px; color: #666;")
        column.addWidget(self.status_label)

        column.addStretch(1)
        return panel

    def _build_right_column(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        head = QHBoxLayout()
        self.count_label = QLabel("还没收到图片")
        self.count_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        head.addWidget(self.count_label, 1)
        self.use_button = QPushButton("用这张图去录入")
        self.use_button.setMinimumWidth(130)
        self.use_button.setEnabled(False)
        self.use_button.clicked.connect(self._use_selected)
        head.addWidget(self.use_button)
        column.addLayout(head)

        self.list = QListWidget()
        self.list.setFrameShape(QFrame.StyledPanel)
        self.list.setStyleSheet("QListWidget::item { padding: 8px 6px; }")
        self.list.itemSelectionChanged.connect(self._sync_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self._use_selected())
        column.addWidget(self.list, 1)

        note = QLabel("收到的是原图，点「用这张图去录入」会跳到录入页并自动填好图片。")
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 12px; color: #888;")
        column.addWidget(note)
        return panel

    # ------------------------------------------------------------- 启停
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        super().showEvent(event)
        if self._auto_start and not self._auto_started:
            self._auto_started = True
            self._start()

    def _start(self) -> None:
        try:
            upload_service.start_server()
        except UploadFailed as exc:
            self._show_banner(f"{exc.user_message}", "error")
            self._refresh_state()
            return

        self._show_banner(
            "服务已启动。手机扫码打开页面即可上传，图片每秒自动同步一次。",
            "ok",
        )
        self._refresh_state()
        self.message.emit(f"手机上传服务已启动：{upload_service.base_url()}")

    def _stop(self) -> None:
        upload_service.stop_server()
        self._show_banner("服务已停止，手机现在连不上了。", "info")
        self._refresh_state()
        self.message.emit("手机上传服务已停止")

    def _copy_url(self) -> None:
        if not upload_service.is_running():
            self._show_banner("服务还没启动，没有可复制的地址。", "error")
            return
        QApplication.clipboard().setText(upload_service.base_url())
        self._show_banner("地址已复制到剪贴板。", "info")

    # ------------------------------------------------------------- 状态刷新
    def _refresh_state(self) -> None:
        running = upload_service.is_running()

        if running:
            url = upload_service.base_url()
            self.url_label.setText(url)
            self.qr_label.setPixmap(_qr_pixmap(url))
            self.status_label.setText(
                f"正在监听 {upload_service.port()} 端口，收到 {len(upload_service.INBOX)} 张。"
                "手机打不开时，先确认两台设备在同一个 Wi-Fi，再检查防火墙。"
            )
        else:
            self.url_label.setText("服务未启动")
            self.qr_label.clear()
            self.qr_label.setText("服务启动后显示二维码")
            self.status_label.setText(
                f"点「启动服务」后，手机访问 http://{upload_service.lan_ip()}:"
                f"{config.get_settings().upload_port} 即可。"
            )

        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.copy_button.setEnabled(running)
        self._last_running = running
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.use_button.setEnabled(self.list.currentItem() is not None)

    # ------------------------------------------------------------- 轮询
    def _poll(self) -> None:
        version = upload_service.INBOX.version()
        if version != self._seen_version:
            self._seen_version = version
            self._reload_list()

        # 服务可能在别处被停掉（或启动失败后自己退了），状态跟着变
        if upload_service.is_running() != self._last_running:
            self._refresh_state()

    def _reload_list(self) -> None:
        items = upload_service.INBOX.snapshot()
        current = self.list.currentItem()
        keep_path = current.data(Qt.UserRole) if current is not None else None

        self.list.clear()
        for received in reversed(items):
            item = QListWidgetItem(
                f"{received.clock_text}　{received.size_text}　{received.original_name}"
            )
            item.setData(Qt.UserRole, received.relative_path)
            item.setToolTip(f"{received.relative_path}\n{received.received_at}")
            self.list.addItem(item)
            if keep_path == received.relative_path:
                self.list.setCurrentItem(item)

        # 没有保持原来选中项时，默认选中最新的那张，"用这张图去录入"立刻可用
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)

        total = len(items)
        self.count_label.setText(f"已收到 {total} 张" if total else "还没收到图片")
        if total:
            latest = items[-1]
            self._show_banner(
                f"刚收到 1 张：{latest.original_name}（{latest.size_text}），"
                "选中后点「用这张图去录入」。",
                "ok",
            )
        self._refresh_state()

    # ------------------------------------------------------------- 取图
    def _use_selected(self) -> None:
        item = self.list.currentItem()
        if item is None:
            self._show_banner("先在右边列表里点一张图。", "error")
            return

        relative = item.data(Qt.UserRole)
        absolute = entry_service.absolute_image_path(relative)
        if absolute is None:
            self._show_banner("这张图在磁盘上找不到了，可能被手动删了。", "error")
            return

        self.image_chosen.emit(str(absolute))
        self.message.emit(f"已把 {Path(absolute).name} 送去录入")

    # ------------------------------------------------------------- 杂项
    def _show_banner(self, text: str, level: str = "info") -> None:
        self._banner.setText(text)
        self._banner.setStyleSheet(
            "padding: 9px 12px; border-radius: 6px; " + _BANNER_STYLES.get(level, "")
        )
        self._banner.setVisible(True)


__all__ = ["UploadPage", "POLL_INTERVAL_MS"]