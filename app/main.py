"""应用入口。

职责：准备目录、建/检查数据库、把主窗口开起来，再把托盘和"到点提醒"
这两条后台线索接上。界面细节在 app/ui/，业务在 app/services/。

`AppShell` 单独拎出来是为了：提醒的定时器、托盘、提醒小窗三者要互相认识，
但它们都不该知道彼此的实现——壳子负责转接信号，也方便在测试里手动
调 `_tick(now=...)` 验证"到点会弹、已打卡不弹"。
"""

from __future__ import annotations

import sys
from datetime import datetime

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from app import config
from app.logger import get_logger, log_exception, setup_logging
from app.services import checkin_service, reminder_service, upload_service
from app.storage import backup
from app.storage.db import StorageError, init_db
from app.ui import tray as tray_module
from app.ui.main_window import WINDOW_TITLE, MainWindow
from app.ui.reminder_popup import ReminderPopup

log = get_logger("main")

# 一分钟看一次就够了：提醒时间是分钟级的，没必要每秒醒一次
CHECK_INTERVAL_MS = 60_000
# 启动后先等一会儿再第一次检查，免得刚开机就弹窗盖住别的窗口
FIRST_CHECK_DELAY_MS = 3_000


class AppShell(QObject):
    """把主窗口、托盘、提醒小窗、定时器接到一起。"""

    def __init__(
        self,
        app: QApplication,
        window: MainWindow,
        settings: config.Settings,
        *,
        interval_ms: int = CHECK_INTERVAL_MS,
    ) -> None:
        super().__init__(window)
        self._app = app
        self._window = window
        self._settings = settings

        self.gate = reminder_service.ReminderGate(settings.reminder_time)
        # 取"现在几点"的地方集中在一处：测试可以把它替换成固定时间
        self.now_fn = datetime.now
        self.popup = ReminderPopup(snooze_minutes=reminder_service.DEFAULT_SNOOZE_MINUTES)
        self.tray = tray_module.Tray(self)

        # 有托盘才允许"关窗不退出"，否则用户会以为程序没关掉
        self.tray_available = self.tray.available()
        window.set_minimize_to_tray(self.tray_available)

        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self._tick)

        self._connect()
        self._refresh_tray_state()

    # ------------------------------------------------------------- 接线
    def _connect(self) -> None:
        self.tray.open_requested.connect(self._show_window)
        self.tray.checkin_requested.connect(self._go_checkin)
        self.tray.quit_requested.connect(self._quit)

        self._window.hidden_to_tray.connect(self._on_hidden_to_tray)
        self._window.checkin_page.checked_in.connect(self._on_checked_in)

        self.popup.checkin_requested.connect(self._go_checkin)
        self.popup.snooze_requested.connect(self._on_snooze)
        self.popup.dismissed.connect(self._on_dismissed)

        # 兜底：不管从哪条路退出（托盘、系统关机、Ctrl+C），先把置顶小窗和托盘图标收掉。
        # 实测"小窗还开着时直接 app.quit()"，事件循环会卡住不返回。
        self._app.aboutToQuit.connect(self._on_about_to_quit)

    def start(self) -> None:
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, self._tick)
        self.timer.start()

    # ------------------------------------------------------------- 提醒
    def _tick(self, now: datetime | None = None) -> None:
        moment = now or self.now_fn()
        checked = self._checked_today()
        if self.gate.should_notify(moment, checked_today=checked):
            if self._show_reminder():
                self.gate.mark_notified(moment)

    def _show_reminder(self) -> bool:
        try:
            status = checkin_service.status()
        except checkin_service.CheckinFailed:
            status = None  # 库有问题也照样提醒，弹窗里退化成一句提示
        try:
            self.popup.show_reminder(status)
        except Exception:  # 弹窗失败不该影响别的功能
            log.exception("提醒小窗弹不出来")
            return False
        return True

    def _checked_today(self) -> bool:
        try:
            return checkin_service.status().checked_today
        except checkin_service.CheckinFailed:
            # 库读不出来时宁可静默，不要每小时弹一次点了就报错的提醒
            return True

    def _on_snooze(self) -> None:
        until = self.gate.snooze(self.now_fn())
        self._window.statusBar().showMessage(
            f"好，{until:%H:%M} 再提醒你。", 5000
        )

    def _on_dismissed(self) -> None:
        # 点 × 等于"今天先算了"，记成已提醒，今天不再自动弹
        self.gate.mark_notified(self.now_fn())

    def _on_checked_in(self, status=None) -> None:
        """刚打完卡：关掉还开着的提醒小窗，托盘状态同步，今晚不再提醒。"""
        self.popup.hide()
        self.gate.on_checked()
        self._refresh_tray_state()

    # ------------------------------------------------------------- 托盘
    def _refresh_tray_state(self) -> None:
        checked = self._checked_today()
        self.tray.set_checked_today(checked)
        self.tray.set_summary(self._summary_text(checked))

    def _summary_text(self, checked: bool) -> str:
        try:
            status = checkin_service.status()
        except checkin_service.CheckinFailed:
            return "日语错题本"
        head = "今天已打卡" if checked else f"{self.gate.reminder_text} 提醒打卡"
        return f"{head}　·　{status.streak_text}"

    def _show_window(self) -> None:
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()
        self._refresh_tray_state()

    def _go_checkin(self) -> None:
        self.popup.hide()
        self._window.open_checkin_page()
        self._refresh_tray_state()

    def _on_hidden_to_tray(self) -> None:
        self.tray.show_message(
            "还在后台跑着",
            "手机上传、到点提醒都照常。要完全退出请用托盘菜单里的「退出」。",
        )

    def _quit(self) -> None:
        log.info("退出：从托盘菜单退出")
        self.popup.hide()
        self.tray.close()
        self.timer.stop()
        self._window.request_quit()
        self._app.quit()

    def _on_about_to_quit(self) -> None:
        self.timer.stop()
        self.popup.hide()
        self.tray.close()


def main() -> int:
    config.ensure_dirs()
    setup_logging()
    log.info("启动：数据库 %s", config.DB_PATH)

    # 先给"不可再生的东西"上保险：完整性检查 + 每天一份快照（失败只记日志，不拦启动）
    log.info("%s", backup.startup_prepare())

    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)
    # 关窗口最小化到托盘，所以"最后一个窗口关了"不等于要退出
    app.setQuitOnLastWindowClosed(False)
    # 退出时把上传服务停掉，端口立刻释放，下次启动不会"端口被占"
    app.aboutToQuit.connect(upload_service.stop_server)

    try:
        init_db()
    except StorageError as exc:
        log_exception(log, "数据库初始化失败", exc)
        QMessageBox.critical(
            None,
            WINDOW_TITLE,
            f"数据库初始化失败：{exc}\n\n"
            "请检查 data 目录是否可写。如果是旧版本程序建的库，"
            "请先备份 data/app.db 再删除它（程序会重新建一个空库）。",
        )
        return 1

    settings = config.get_settings()
    window = MainWindow(settings)
    window.show()

    shell = AppShell(app, window, settings)
    if not shell.tray_available:
        # 没有托盘就别开着窗口消失、程序还在——直接恢复"关窗即退出"
        app.setQuitOnLastWindowClosed(True)
        window.set_minimize_to_tray(False)
    else:
        shell.start()

    # 壳子要活到进程结束，挂到 app 上防止被回收
    app._shell = shell  # type: ignore[attr-defined]
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())