"""阶段 7 验收（二）：提醒小窗、托盘、关窗收进托盘、到点自动弹。

全程 offscreen，不联网、不占端口：
* 手机上传服务被换成假实现（只为了让主窗口能建起来）；
* 打卡状态用真实临时库 + 真实 checkin_service；
* "现在几点"由测试自己传进去（`AppShell._tick(now=...)`），不靠等真时钟。
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app import config  # noqa: E402
from app.services import checkin_service, upload_service  # noqa: E402
from app.storage.db import init_db  # noqa: E402

WORK_DIR = config.DATA_DIR / "_verify_reminder_ui"
UPLOAD_ROOT = WORK_DIR / "uploads"
VERIFY_DB = WORK_DIR / "verify.db"

_passed = 0
_failed = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"[PASS] {name}")
    else:
        _failed += 1
        print(f"[FAIL] {name}" + (f" -> {extra}" if extra else ""))


def eq(name: str, got, want) -> None:
    check(name, got == want, f"got={got!r} want={want!r}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


class FakeServer:
    """把上传服务换成假的：不绑端口，也不去取真实 IP。"""

    def __init__(self) -> None:
        self.started = False
        self.base_url_called = 0

    def start_server(self, port=None, *, inbox=None, host="0.0.0.0"):
        self.started = True

    def stop_server(self) -> None:
        self.started = False

    def is_running(self) -> bool:
        return self.started

    def port(self) -> int:
        return 8765

    def lan_ip(self) -> str:
        return "127.0.0.1"

    def base_url(self) -> str:
        self.base_url_called += 1
        return "http://127.0.0.1:8765"


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    config.DATA_DIR = WORK_DIR
    config.UPLOAD_DIR = UPLOAD_ROOT
    config.DB_PATH = VERIFY_DB
    init_db(VERIFY_DB)

    fake = FakeServer()
    upload_service.start_server = fake.start_server  # type: ignore[assignment]
    upload_service.stop_server = fake.stop_server  # type: ignore[assignment]
    upload_service.is_running = fake.is_running  # type: ignore[assignment]
    upload_service.port = fake.port  # type: ignore[assignment]
    upload_service.lan_ip = fake.lan_ip  # type: ignore[assignment]
    upload_service.base_url = fake.base_url  # type: ignore[assignment]

    app = QApplication.instance() or QApplication([])
    print(f"临时库: {VERIFY_DB}")

    # 延迟导入：上面的 monkeypatch 要在建窗口之前生效
    from app.main import AppShell  # noqa: E402
    from app.ui import tray as tray_module  # noqa: E402
    from app.ui.main_window import PAGE_CHECKIN, MainWindow  # noqa: E402
    from app.ui.reminder_popup import ReminderPopup  # noqa: E402

    settings = config.Settings(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        vision_model="test-model",
        upload_port=8765,
        reminder_time="21:00",
    )

    section("1. 提醒小窗：有数据时的文案")
    popup = ReminderPopup(snooze_minutes=10)
    status = checkin_service.status()
    popup.show_reminder(status)
    app.processEvents()
    check("窗口弹出来了", popup.isVisible())
    eq("标题", popup.title_label.text(), "该学日语了")
    eq("连续天数为 0 时的文案", popup.streak_label.text(), "还没有连续记录")
    check("详情里有今天状态", "今天还没打卡" in popup.detail_label.text(), popup.detail_label.text())
    check("详情里有累计", "累计" in popup.detail_label.text(), popup.detail_label.text())
    eq("打卡按钮文案", popup.checkin_button.text(), "现在去打卡")
    eq("延后按钮文案带分钟数", popup.snooze_button.text(), "10 分钟后再提醒")
    check("窗口宽度固定 340", popup.width() == 340, str(popup.width()))
    from PySide6.QtCore import Qt

    check("置顶（WindowStaysOnTopHint）", bool(popup.windowFlags() & Qt.WindowStaysOnTopHint))
    check("无边框（FramelessWindowHint）", bool(popup.windowFlags() & Qt.FramelessWindowHint))
    check("不占任务栏（Qt.Tool）", popup.windowFlags() & Qt.WindowType.Tool == Qt.WindowType.Tool)

    section("2. 提醒小窗：点按钮发信号并收起")
    got = {"checkin": 0, "snooze": 0, "dismissed": 0}
    popup.checkin_requested.connect(lambda: got.__setitem__("checkin", got["checkin"] + 1))
    popup.snooze_requested.connect(lambda: got.__setitem__("snooze", got["snooze"] + 1))
    popup.dismissed.connect(lambda: got.__setitem__("dismissed", got["dismissed"] + 1))

    popup.checkin_button.click()
    app.processEvents()
    eq("点「现在去打卡」发了一次信号", got["checkin"], 1)
    check("点完自己收起来", not popup.isVisible())

    popup.show_reminder(status)
    popup.snooze_button.click()
    app.processEvents()
    eq("点「10 分钟后再提醒」发了一次信号", got["snooze"], 1)
    check("点完也收起来", not popup.isVisible())

    popup.show_reminder(status)
    popup.close_button.click()
    app.processEvents()
    eq("点 × 发 dismissed", got["dismissed"], 1)

    section("3. 提醒小窗：没拿到状态时也不空白")
    popup.show_reminder(None)
    app.processEvents()
    eq("退化成一句提示", popup.streak_label.text(), "今天还没打卡")
    check("详情给出去哪儿打卡", "打卡" in popup.detail_label.text(), popup.detail_label.text())
    popup.set_snooze_minutes(20)
    eq("改延后分钟数", popup.snooze_button.text(), "20 分钟后再提醒")
    popup.hide()

    section("4. 提醒小窗：右下角定位")
    popup.show_reminder(status)
    app.processEvents()
    screen = app.primaryScreen().availableGeometry()
    check("靠着右边缘（24px 留白）",
          abs((screen.x() + screen.width() - popup.width() - 24) - popup.x()) <= 2,
          f"x={popup.x()} screen_right={screen.x() + screen.width()}")
    check("靠着下边缘（24px 留白）",
          abs((screen.y() + screen.height() - popup.height() - 24) - popup.y()) <= 2,
          f"y={popup.y()} screen_bottom={screen.y() + screen.height()}")
    popup.hide()

    section("5. 托盘：菜单项与状态")
    tray = tray_module.Tray()
    print(f"（本环境的托盘可用性：{tray.available()}）")
    eq("打开主窗口", tray.open_action.text(), "打开主窗口")
    eq("去打卡", tray.checkin_action.text(), "去打卡")
    check("默认可以去打卡", tray.checkin_action.isEnabled())
    eq("退出", tray.quit_action.text(), "退出")
    eq("悬浮提示默认", tray.icon.toolTip(), "日语错题本")

    tray.set_checked_today(True)
    eq("打完卡变成确认文案", tray.checkin_action.text(), "今天已打卡 ✓")
    check("打完卡点不动", not tray.checkin_action.isEnabled())
    check("记住已打卡", tray.checked_today)
    tray.set_checked_today(False)
    check("退回可点状态", tray.checkin_action.isEnabled())

    fired = {"open": 0, "checkin": 0, "quit": 0}
    tray.open_requested.connect(lambda: fired.__setitem__("open", fired["open"] + 1))
    tray.checkin_requested.connect(lambda: fired.__setitem__("checkin", fired["checkin"] + 1))
    tray.quit_requested.connect(lambda: fired.__setitem__("quit", fired["quit"] + 1))
    tray.open_action.trigger()
    tray.checkin_action.trigger()
    tray.set_checked_today(True)
    tray.checkin_action.trigger()  # 已禁用，不该再发信号
    tray.quit_action.trigger()
    app.processEvents()
    eq("打开主窗口发信号", fired["open"], 1)
    eq("去打卡发信号（禁用时不再发）", fired["checkin"], 1)
    eq("退出发信号", fired["quit"], 1)
    tray.set_checked_today(False)

    tray.set_summary("今天已打卡　·　已连续 3 天")
    eq("悬浮提示可改", tray.icon.toolTip(), "今天已打卡　·　已连续 3 天")
    tray.set_summary("")
    eq("空串回落默认", tray.icon.toolTip(), "日语错题本")
    check("没有托盘时 show_message 返回 False 而不崩", tray.show_message("x", "y") in (True, False))

    section("6. 主窗口：关窗口收进托盘 / 真退出")
    window = MainWindow(settings)
    window.show()
    app.processEvents()
    hidden = {"count": 0}
    window.hidden_to_tray.connect(lambda: hidden.__setitem__("count", hidden["count"] + 1))

    window.set_minimize_to_tray(False)
    window.close()
    app.processEvents()
    check("没托盘时关窗就是关窗", not window.isVisible())
    eq("没托盘时不该发 hidden_to_tray", hidden["count"], 0)

    window.show()
    window.set_minimize_to_tray(True)
    app.processEvents()
    window.close()
    app.processEvents()
    check("有托盘时关窗后窗口不可见", not window.isVisible())
    eq("发了 hidden_to_tray", hidden["count"], 1)
    check("但窗口对象没被销毁", window.isWindow())

    window.request_quit()
    app.processEvents()
    eq("request_quit 不再触发收进托盘", hidden["count"], 1)

    section("7. 主窗口：open_checkin_page 跳转")
    window2 = MainWindow(settings)
    window2.show()
    window2.open_checkin_page()
    app.processEvents()
    eq("切到了打卡页", window2._stack.currentIndex(), PAGE_CHECKIN)
    check("标题跟着变", "打卡" in window2.windowTitle(), window2.windowTitle())
    window2.set_minimize_to_tray(False)
    window2.close()

    section("8. AppShell：到点才弹、已打卡不弹")
    window3 = MainWindow(settings)
    window3.show()
    app.processEvents()
    shell = AppShell(app, window3, settings)
    shell.timer.stop()  # 不用真定时器，测试自己调 _tick
    clock = {"now": datetime(2026, 11, 2, 21, 0)}
    shell.now_fn = lambda: clock["now"]  # 让"现在几点"可控
    shell.tray.set_checked_today(False)
    shell._tick(datetime(2026, 11, 2, 20, 59))
    app.processEvents()
    check("20:59 不弹", not shell.popup.isVisible())

    shell._tick(datetime(2026, 11, 2, 21, 0))
    app.processEvents()
    check("21:00 弹出来", shell.popup.isVisible())
    eq("记成今天提醒过", shell.gate.notified_on, date(2026, 11, 2))

    shell._tick(datetime(2026, 11, 2, 21, 1))
    app.processEvents()
    check("同一分钟再 tick 不会重复弹（窗口本来就开着）", shell.popup.isVisible())

    # 关掉小窗后再 tick：今天已经提醒过，不能再弹
    shell.popup.hide()
    shell._tick(datetime(2026, 11, 2, 22, 0))
    app.processEvents()
    check("今天提醒过就不再弹", not shell.popup.isVisible())

    section("9. AppShell：点「10 分钟后再提醒」")
    shell.gate.reset()
    clock["now"] = datetime(2026, 11, 2, 21, 0)
    shell._tick(datetime(2026, 11, 2, 21, 0))
    app.processEvents()
    check("先弹一次", shell.popup.isVisible())
    shell.popup.snooze_button.click()
    app.processEvents()
    check("点了延后，小窗收起", not shell.popup.isVisible())
    eq("延到 21:10", shell.gate.snoozed_until, datetime(2026, 11, 2, 21, 10))
    clock["now"] = datetime(2026, 11, 2, 21, 5)
    shell._tick(clock["now"])
    app.processEvents()
    check("21:05 还在延后期", not shell.popup.isVisible())
    clock["now"] = datetime(2026, 11, 2, 21, 10)
    shell._tick(clock["now"])
    app.processEvents()
    check("21:10 重新弹", shell.popup.isVisible())

    section("10. AppShell：打卡之后就安静了")
    shell.popup.hide()
    shell.gate.reset()
    status, newly = checkin_service.check_today("验收")
    eq("打卡成功", newly, True)
    shell._on_checked_in(status)
    check("托盘变成已打卡", shell.tray.checked_today)
    shell._tick(datetime(2026, 11, 2, 21, 30))
    app.processEvents()
    check("已打卡就不再弹", not shell.popup.isVisible())
    check("托盘提示写着已打卡", "今天已打卡" in shell.tray.icon.toolTip(), shell.tray.icon.toolTip())

    section("11. AppShell：从提醒小窗跳去打卡页")
    shell.gate.reset()
    shell.gate._notified_on = None  # 模拟"今天还没提醒过"
    window3._nav.setCurrentRow(0)
    shell.popup.show_reminder(None)
    shell.popup.checkin_button.click()
    app.processEvents()
    eq("切到打卡页", window3._stack.currentIndex(), PAGE_CHECKIN)
    check("小窗收了", not shell.popup.isVisible())

    section("12. AppShell：启动/退出")
    shell.start()
    check("定时器起来了", shell.timer.isActive())
    shell.timer.stop()
    shell._quit()
    app.processEvents()
    check("退出后定时器停了", not shell.timer.isActive())

    # 真实平台上"置顶小窗还开着时直接退出"会让事件循环卡住不返回，
    # 所以退出路径必须先收小窗；这里守住这个约定。
    from PySide6.QtWidgets import QApplication as _QApp

    app2 = _QApp.instance()
    shell2 = AppShell(app2, window3, settings)
    shell2.timer.stop()
    shell2.popup.show_reminder(None)
    app.processEvents()
    check("退出前小窗开着", shell2.popup.isVisible())
    shell2.start()
    shell2._quit()
    app.processEvents()
    check("退出时先把小窗收起来了", not shell2.popup.isVisible())
    check("退出时定时器停掉了", not shell2.timer.isActive())

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())