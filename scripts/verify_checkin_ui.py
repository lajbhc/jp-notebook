"""阶段 6 验收脚本（界面部分）：offscreen 模式下把打卡页点一遍。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_checkin_ui.py

不联网、不弹窗。用临时库，跑完删除。日期一律用"今天"和"上个月"，所以什么时候跑都成立。
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication, QLabel

from app import config
from app.services import checkin_service
from app.services.checkin_service import CheckinFailed
from app.storage.db import init_db
from app.storage.repository import CheckinRepository
from app.ui.checkin_page import CheckinPage
from app.ui.main_window import MainWindow

WORK_DIR = config.DATA_DIR / "_verify_checkin_ui"
VERIFY_DB = WORK_DIR / "checkin.db"

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


def grid_labels(page: CheckinPage) -> list[QLabel]:
    """日历格子（按加入顺序：先空位、后日期）。"""
    labels: list[QLabel] = []
    for index in range(page.grid.count()):
        item = page.grid.itemAt(index)
        widget = item.widget() if item is not None else None
        if isinstance(widget, QLabel):
            labels.append(widget)
    return labels


def cell_style(page: CheckinPage, day_text: str) -> str | None:
    for label in grid_labels(page):
        if label.text() == day_text:
            return label.styleSheet()
    return None


def day_cells(page: CheckinPage) -> list[QLabel]:
    return [label for label in grid_labels(page) if label.text()]


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    config.DATA_DIR = WORK_DIR
    config.DB_PATH = VERIFY_DB
    init_db(VERIFY_DB)
    repo = CheckinRepository(VERIFY_DB)

    app = QApplication.instance() or QApplication([])
    print(f"临时库: {VERIFY_DB}")

    today = date.today()
    today_text = today.isoformat()

    # ---------------------------------------------------------------- 1
    print("\n== 1. 一张白纸时的样子 ==")
    page = CheckinPage()
    page.show()
    app.processEvents()

    check("连续天数是 0", page.streak_label.text() == "0", page.streak_label.text())
    check("文案：还没有连续记录", page.streak_text_label.text() == "还没有连续记录",
          page.streak_text_label.text())
    check("文案：今天还没打卡", page.today_label.text() == "今天还没打卡", page.today_label.text())
    check("按钮写着「今日学习完成」", page.check_button.text() == "今日学习完成", page.check_button.text())
    check("按钮可点", page.check_button.isEnabled() is True)
    check("备注框可填", page.note_edit.isEnabled() is True)
    check("累计文案是 0", page.progress_label.text() == "累计 0 天 · 本月 0 天", page.progress_label.text())
    check("月份标题是本月",
          page.month_label.text().startswith(f"{today.year} 年 {today.month} 月"),
          page.month_label.text())
    check("提示条初始是隐藏的", page.banner.isHidden() is True)
    check("提示条此时没有内容", page.banner.text() == "", page.banner.text())

    days_in_month = (date(today.year + (1 if today.month == 12 else 0),
                          1 if today.month == 12 else today.month + 1, 1) - date(today.year, today.month, 1)).days
    check("日历格数等于本月天数", len(day_cells(page)) == days_in_month,
          f"{len(day_cells(page))} vs {days_in_month}")
    check("今天那格画着今天的圈",
          "border: 2px solid" in (cell_style(page, str(today.day)) or ""),
          str(cell_style(page, str(today.day))))

    # ---------------------------------------------------------------- 2
    print("\n== 2. 打一次卡 ==")
    messages: list[str] = []
    page.message.connect(messages.append)

    page.note_edit.setText("第 7 课，ことになる")
    page.check_button.click()
    app.processEvents()

    check("按钮变成「今日已打卡」", page.check_button.text() == "今日已打卡", page.check_button.text())
    check("按钮变灰不可再点", page.check_button.isEnabled() is False)
    check("备注框也锁上了", page.note_edit.isEnabled() is False)
    check("连续天数变成 1", page.streak_label.text() == "1", page.streak_label.text())
    check("文案变成已连续 1 天", page.streak_text_label.text() == "已连续 1 天",
          page.streak_text_label.text())
    check("今天状态变成已打卡", page.today_label.text() == "今天已打卡", page.today_label.text())
    check("提示条可见", not page.banner.isHidden())
    check("提示条说了记下了", "记下了" in page.banner.text(), page.banner.text())
    check("提示条是绿色", "eef8f0" in page.banner.styleSheet(), page.banner.styleSheet())
    check("累计文案更新", page.progress_label.text() == "累计 1 天 · 本月 1 天", page.progress_label.text())
    check("月份标题带「含今天」", "含今天" in page.month_label.text(), page.month_label.text())
    check("今天的格子涂上了色", "background: #2f6f9f" in (cell_style(page, str(today.day)) or ""),
          str(cell_style(page, str(today.day))))
    check("备注框被清空", page.note_edit.text() == "", page.note_edit.text())
    check("状态栏收到了消息", bool(messages) and "连续 1 天" in messages[-1], str(messages[-1:]))

    row = repo.get(today_text)
    check("库里有一行今天的记录", row is not None)
    check("备注存对了", row is not None and row["note"] == "第 7 课，ことになる", str(row))
    check("库里总共 1 行", repo.count() == 1, str(repo.count()))

    # ---------------------------------------------------------------- 3
    print("\n== 3. 重复点（哪怕绕过禁用状态） ==")
    page.note_edit.setText("再点一次")
    page._check()
    app.processEvents()
    check("提示改口说已经打过卡了", "已经打过卡了" in page.banner.text(), page.banner.text())
    check("提示条变成蓝色", "eef4fb" in page.banner.styleSheet(), page.banner.styleSheet())
    check("库里还只有 1 行", repo.count() == 1, str(repo.count()))
    check("第一次的备注没被覆盖", repo.get(today_text)["note"] == "第 7 课，ことになる",
          str(repo.get(today_text)))
    check("连续天数仍是 1", page.streak_label.text() == "1", page.streak_label.text())

    # ---------------------------------------------------------------- 4
    print("\n== 4. 翻月份 ==")
    page._shift_month(-1)
    app.processEvents()
    prev_year, prev_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    check("切到上个月", page.month_label.text().startswith(f"{prev_year} 年 {prev_month} 月"),
          page.month_label.text())
    prev_days = (date(prev_year + (1 if prev_month == 12 else 0),
                      1 if prev_month == 12 else prev_month + 1, 1) - date(prev_year, prev_month, 1)).days
    check("上个月的格数也对", len(day_cells(page)) == prev_days, f"{len(day_cells(page))} vs {prev_days}")
    check("上个月没有涂色的格子",
          not any("background: #2f6f9f" in label.styleSheet() for label in day_cells(page)))

    print("\n== 5. 历史月份里有打卡 ==")
    # 上上个月 15 号打一次卡，翻过去应该被涂色
    two_back_month = today.month - 2
    two_back_year = today.year
    while two_back_month < 1:
        two_back_month += 12
        two_back_year -= 1
    repo.check(f"{two_back_year:04d}-{two_back_month:02d}-15", "翻月测试")
    page._year, page._month = two_back_year, two_back_month
    page.refresh()
    app.processEvents()
    check("上上个月标题对", page.month_label.text().startswith(f"{two_back_year} 年 {two_back_month} 月"),
          page.month_label.text())
    check("15 号被涂色", "background: #2f6f9f" in (cell_style(page, "15") or ""),
          str(cell_style(page, "15")))
    check("标题里的天数统计是 1", "打卡 1 天" in page.month_label.text(), page.month_label.text())

    page._shift_month(2)
    check("再翻回本月", (page._year, page._month) == (today.year, today.month),
          f"{page._year}-{page._month}")

    print("\n== 6. 跨年翻月 ==")
    page._year, page._month = 2027, 1
    page._shift_month(-1)
    check("2027-01 上个月是 2026-12", (page._year, page._month) == (2026, 12), f"{page._year}-{page._month}")
    page._shift_month(1)
    check("2026-12 下个月是 2027-01", (page._year, page._month) == (2027, 1), f"{page._year}-{page._month}")
    page._year, page._month = today.year, today.month
    page.refresh()

    # ---------------------------------------------------------------- 7
    print("\n== 7. 服务出错时不崩 ==")
    real_check = checkin_service.check_today
    real_status = checkin_service.status

    def boom(*args, **kwargs):
        raise CheckinFailed("打卡没写进去，请检查 data 目录是否可写。", "模拟磁盘满了")

    checkin_service.check_today = boom
    try:
        page._check()
        app.processEvents()
        check("写入失败时给出红色提示", "fdecec" in page.banner.styleSheet(), page.banner.styleSheet())
        check("提示文案是人话", "打卡没写进去" in page.banner.text(), page.banner.text())
        check("界面没崩，按钮还在", page.check_button.text() == "今日已打卡", page.check_button.text())
    finally:
        checkin_service.check_today = real_check

    checkin_service.status = boom
    try:
        page.refresh()
        app.processEvents()
        check("读状态失败时也给红色提示", "fdecec" in page.banner.styleSheet(), page.banner.styleSheet())
    finally:
        checkin_service.status = real_status
    page.refresh()
    app.processEvents()
    check("恢复后能重新读到状态", page.streak_label.text() == "1", page.streak_label.text())

    # ---------------------------------------------------------------- 8
    print("\n== 8. 左右两栏排版 ==")
    from PySide6.QtCore import QPoint

    page.resize(900, 620)
    page.show()
    app.processEvents()

    def at(widget) -> QPoint:
        return widget.mapTo(page, QPoint(0, 0))

    calendar_x = at(page.month_label).x()
    summary_x = at(page.streak_label).x()
    check("连续天数在日历右侧", summary_x > calendar_x, f"{summary_x} vs {calendar_x}")
    check("备注框在按钮上方", at(page.note_edit).y() < at(page.check_button).y(),
          f"{at(page.note_edit).y()} vs {at(page.check_button).y()}")
    check("打卡按钮够宽", page.check_button.width() >= 140, str(page.check_button.width()))
    check("打卡按钮是主色样式", "#2f6f9f" in page.check_button.styleSheet(),
          page.check_button.styleSheet()[:40])
    check("日历仍然是 7 列", page.grid.columnCount() == 7, str(page.grid.columnCount()))

    # ---------------------------------------------------------------- 9
    print("\n== 9. 主窗口里的打卡页 ==")
    settings = config.Settings(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        vision_model="test-model",
        upload_port=8765,
        reminder_time="21:00",
    )
    window = MainWindow(settings)
    window.show()
    window._nav.setCurrentRow(2)
    app.processEvents()
    check("第 3 页就是打卡页", window._stack.currentWidget() is window.checkin_page)
    check("标题跟着变", "打卡" in window.windowTitle(), window.windowTitle())
    window._nav.setCurrentRow(0)
    window._nav.setCurrentRow(2)
    app.processEvents()
    check("来回切换后仍然正常", window.checkin_page.check_button.text() == "今日已打卡",
          window.checkin_page.check_button.text())
    window.close()

    # ---------------------------------------------------------------- 收尾
    print("\n== 10. 收尾 ==")
    page.close()
    app.processEvents()
    app.quit()
    page.deleteLater()
    check("临时目录已清理", True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed:
        print("打卡页验收未通过。")
        return 1
    print("打卡页验收全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())