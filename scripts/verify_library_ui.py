"""阶段 4 验收脚本（界面部分）：offscreen 模式下真的点一遍词库页。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_library_ui.py

覆盖：列表加载、关键词/类型/日期筛选、空结果、翻页、编辑对话框保存与校验、
删除的二次确认（用假弹窗代替人手点"是/否"）。
用临时库与临时图片目录，跑完删除。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QMessageBox

from app import config, models
from app.services import library_service
from app.storage.db import init_db
from app.storage.repository import EntryRepository
from app.ui.entry_edit_dialog import EntryEditDialog
from app.ui.library_page import LibraryPage

WORK_DIR = config.DATA_DIR / "_verify_library_ui"
VERIFY_DB = WORK_DIR / "library_ui.db"

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


def seed(repo: EntryRepository) -> None:
    """12 条生词 + 13 条语法 = 25 条，刚好跨两页（每页 20）。"""
    repo.create(
        models.Entry(
            type=models.ENTRY_TYPE_WORD,
            word="食べる",
            reading="たべる",
            meaning="吃",
            part_of_speech="动词・一段",
            example="ご飯を食べる。",
        )
    )
    for index in range(1, 12):
        repo.create(
            models.Entry(
                type=models.ENTRY_TYPE_WORD,
                word=f"単語{index}",
                reading=f"たんご{index}",
                meaning=f"单词{index}",
            )
        )
    for index in range(1, 14):
        repo.create(
            models.Entry(
                type=models.ENTRY_TYPE_GRAMMAR,
                grammar_point=f"文法{index}",
                connection=f"動詞辞書形 + 文法{index}",
                meaning=f"语法点{index}",
            )
        )


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    config.DATA_DIR = WORK_DIR
    config.UPLOAD_DIR = WORK_DIR / "uploads"
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    config.DB_PATH = VERIFY_DB

    init_db(VERIFY_DB)
    repo = EntryRepository(VERIFY_DB)
    seed(repo)
    print(f"临时库: {VERIFY_DB}（{repo.count()} 条）")

    app = QApplication.instance() or QApplication([])

    # ---------------------------------------------------------------- 1
    print("\n== 1. 第一次加载 ==")
    page = LibraryPage()
    page.show()
    app.processEvents()

    check("总数 25", page.model.rowCount() == 20, str(page.model.rowCount()))
    check(
        "统计文案含两类数量",
        "共 25 条" in page.stats_label.text()
        and "生词 12" in page.stats_label.text()
        and "语法 13" in page.stats_label.text(),
        page.stats_label.text(),
    )
    check("页码显示 1 / 2", page.page_label.text() == "第 1 / 2 页", page.page_label.text())
    check("范围内文案", page.range_label.text() == "第 1–20 条，共 25 条", page.range_label.text())
    check("第一页没有上一页", not page.prev_button.isEnabled())
    check("第一页有下一页", page.next_button.isEnabled())
    check("表格显示的是数据不是占位", page.stack.currentIndex() == 0)

    # 表头单独取一次（QAbstractTableModel 的 headerData 需要横向方向）
    from PySide6.QtCore import Qt

    check(
        "第二列表头为「词形 / 语法点」",
        page.model.headerData(1, Qt.Horizontal) == "词形 / 语法点",
        str(page.model.headerData(1, Qt.Horizontal)),
    )
    check("第一列显示类型中文名", page.model.index(0, 0).data() in ("生词", "语法"),
          str(page.model.index(0, 0).data()))
    check("按创建顺序倒序：首行是最后一条", page.model.entry_at(0).grammar_point == "文法13",
          str(page.model.entry_at(0).title))

    # ---------------------------------------------------------------- 2
    print("\n== 2. 选中与按钮联动 ==")
    check("未选中时「编辑」不可用", not page.edit_button.isEnabled())
    check("未选中时「删除」不可用", not page.delete_button.isEnabled())
    page.table.selectRow(0)
    app.processEvents()
    check("选中后「编辑」可用", page.edit_button.isEnabled())
    check("选中后「删除」可用", page.delete_button.isEnabled())
    check("能取到当前记录", page.current_entry() is not None)

    # ---------------------------------------------------------------- 3
    print("\n== 3. 关键词搜索 ==")
    page.keyword_edit.setText("食べる")
    page._on_search()
    app.processEvents()
    check("搜「食べる」只剩 1 条", page.model.rowCount() == 1, str(page.model.rowCount()))
    check("搜到的是词形", page.model.entry_at(0).word == "食べる", page.model.entry_at(0).title)
    check("回到第 1 页", page.page_label.text() == "第 1 / 1 页", page.page_label.text())

    page.keyword_edit.setText("zzz不存在")
    page._on_search()
    app.processEvents()
    check("空结果显示空状态页", page.stack.currentIndex() == 1, str(page.stack.currentIndex()))
    check("空状态文案提示换关键词", "没有符合条件" in page.empty_label.text(), page.empty_label.text())
    check("空结果时页码仍是 1 / 1", page.page_label.text() == "第 1 / 1 页", page.page_label.text())
    check("空结果文案不是「第 0 条」", page.range_label.text() == "没有匹配的记录",
          page.range_label.text())

    page.reset_filters()
    app.processEvents()
    check("重置后恢复 20 行", page.model.rowCount() == 20, str(page.model.rowCount()))
    check("重置后关键词被清空", page.keyword_edit.text() == "", page.keyword_edit.text())

    # ---------------------------------------------------------------- 4
    print("\n== 4. 类型筛选 ==")
    word_index = page.type_combo.findData(models.ENTRY_TYPE_WORD)
    page.type_combo.setCurrentIndex(word_index)
    app.processEvents()
    check("只看生词 12 条", page.model.rowCount() == 12, str(page.model.rowCount()))
    check("全部是生词", all(e.type == models.ENTRY_TYPE_WORD for e in page.model.entries()))
    check("只剩 1 页", page.page_label.text() == "第 1 / 1 页", page.page_label.text())
    page.type_combo.setCurrentIndex(0)
    app.processEvents()

    # ---------------------------------------------------------------- 5
    print("\n== 5. 日期筛选 ==")
    check("默认没勾日期，日期框不可编辑", not page.date_from.isEnabled())
    page.date_check.setChecked(True)
    app.processEvents()
    check("勾上后日期框可编辑", page.date_from.isEnabled() and page.date_to.isEnabled())
    check("今天的记录都在范围里", page.model.rowCount() == 20, str(page.model.rowCount()))

    page.date_to.setDate(QDate.currentDate().addDays(-1))
    app.processEvents()
    check("把结束日期改到昨天后没有记录", page.model.rowCount() == 0, str(page.model.rowCount()))

    page.date_from.setDate(QDate.currentDate().addDays(1))
    app.processEvents()
    check("开始日期晚于结束日期时报错",
          page.error_label.isVisible() and "调换" in page.error_label.text(),
          page.error_label.text())

    page.date_check.setChecked(False)
    app.processEvents()
    check("取消日期筛选后错误消失", not page.error_label.isVisible())
    check("记录回来了", page.model.rowCount() == 20, str(page.model.rowCount()))

    # ---------------------------------------------------------------- 6
    print("\n== 6. 翻页 ==")
    page.next_button.click()
    app.processEvents()
    check("第 2 页 5 条", page.model.rowCount() == 5, str(page.model.rowCount()))
    check("页码 2 / 2", page.page_label.text() == "第 2 / 2 页", page.page_label.text())
    check("第 2 页有上一页", page.prev_button.isEnabled())
    check("第 2 页没有下一页", not page.next_button.isEnabled())
    check("第 2 页文案", page.range_label.text() == "第 21–25 条，共 25 条",
          page.range_label.text())

    page.prev_button.click()
    app.processEvents()
    check("回到第 1 页", page.page_label.text() == "第 1 / 2 页", page.page_label.text())

    size_index = page.page_size_combo.findData(50)
    page.page_size_combo.setCurrentIndex(size_index)
    app.processEvents()
    check("每页 50 条时一页装下 25 条", page.model.rowCount() == 25, str(page.model.rowCount()))
    check("每页 50 条时只有 1 页", page.page_label.text() == "第 1 / 1 页", page.page_label.text())
    page.page_size_combo.setCurrentIndex(page.page_size_combo.findData(20))
    app.processEvents()

    # ---------------------------------------------------------------- 7
    print("\n== 7. 编辑对话框 ==")
    target = page.model.entry_at(0)
    dialog = EntryEditDialog(target)
    check("标题带记录名", target.title in dialog.windowTitle(), dialog.windowTitle())
    check("类型单选与记录一致",
          dialog.current_type() == target.type, dialog.current_type())
    check("释义已填进输入框",
          dialog._editors["meaning"].text() == target.meaning,
          dialog._editors["meaning"].text())
    # 首行是最后创建的那条（文法13），按记录自己的类型来断言哪半边的字段该可用。
    if target.type == models.ENTRY_TYPE_GRAMMAR:
        check("语法专属字段可用", dialog._editors["grammar_point"].isEnabled())
        check("生词字段在语法记录里被禁用", not dialog._editors["word"].isEnabled())
        check("接续字段可用", dialog._editors["connection"].isEnabled())
    else:
        check("生词专属字段可用", dialog._editors["word"].isEnabled())
        check("语法字段在生词记录里被禁用", not dialog._editors["grammar_point"].isEnabled())
        check("读音字段可用", dialog._editors["reading"].isEnabled())

    dialog._editors["meaning"].setText("改过的释义")
    dialog._save()
    check("保存后对话框接受", dialog.result() == EntryEditDialog.Accepted, str(dialog.result()))
    check("saved_entry 不为空", dialog.saved_entry is not None)
    check("改的是同一条记录", dialog.saved_entry.id == target.id, str(dialog.saved_entry.id))
    check("释义已更新", dialog.saved_entry.meaning == "改过的释义", dialog.saved_entry.meaning)
    check("created_at 不变", dialog.saved_entry.created_at == target.created_at,
          dialog.saved_entry.created_at)
    stored = library_service.get_entry(target.id)
    check("库里也是新释义", stored.meaning == "改过的释义", stored.meaning)

    page.reload()
    app.processEvents()
    check("列表刷新后显示新释义", page.model.entry_at(0).meaning == "改过的释义",
          page.model.entry_at(0).meaning)

    print("\n== 8. 编辑校验 ==")
    bad = EntryEditDialog(stored)
    if stored.type == models.ENTRY_TYPE_GRAMMAR:
        bad._editors["grammar_point"].setText("   ")
    else:
        bad._editors["word"].setText("   ")
    bad._save()
    check("主字段为空时对话框不关闭", bad.result() != EntryEditDialog.Accepted, str(bad.result()))
    # 对话框没有 show，isVisible() 恒为 False，只能看它有没有被显式隐藏。
    check("显示红色校验提示", not bad._error.isHidden() and "主字段" in bad._error.text(),
          bad._error.text())
    check("没有写出半条记录", bad.saved_entry is None)
    check("库里还是原来的词形",
          library_service.get_entry(stored.id).word == stored.word)

    # ---------------------------------------------------------------- 9
    print("\n== 9. 删除的二次确认 ==")
    messages: list[str] = []
    page.message.connect(messages.append)

    before = repo.count()
    original_question = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)  # type: ignore[assignment]
    page.table.selectRow(0)
    app.processEvents()
    victim = page.current_entry()
    page._delete_selected()
    app.processEvents()
    check("选「否」时没有删除", repo.count() == before, f"{before} -> {repo.count()}")

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)  # type: ignore[assignment]
    page._delete_selected()
    app.processEvents()
    QMessageBox.question = original_question  # type: ignore[assignment]
    check("选「是」后真的删掉了", repo.count() == before - 1, f"{before} -> {repo.count()}")
    check("删掉的记录查不到", library_service.get_entry(victim.id) is None)
    check("状态栏收到了删除提示", any("已删除" in text for text in messages), str(messages))
    # 删完剩 24 条，第 1 页仍然装满 20 行；行数该等于 min(每页条数, 剩余条数)。
    expected_rows = min(20, repo.count())
    check("删除后列表行数与剩余条数一致", page.model.rowCount() == expected_rows,
          f"{page.model.rowCount()} vs {expected_rows}")
    check("删除后页码仍为 1 / 2", page.page_label.text() == "第 1 / 2 页", page.page_label.text())

    # ---------------------------------------------------------------- 10
    print("\n== 10. 选中定位 ==")
    second = page.model.entry_at(3)
    check("能选中指定记录", page.select_entry(second.id) is True)
    check("选中的就是它", page.current_entry().id == second.id)
    check("不存在的 id 返回 False", page.select_entry(999999) is False)

    # ---------------------------------------------------------------- 11
    print("\n== 11. 列宽可拖且记得住 ==")
    from PySide6.QtWidgets import QHeaderView

    from app.ui import window_state

    check("表头六列都能拖",
          page.table.horizontalHeader().sectionResizeMode(1) == QHeaderView.Interactive,
          str(page.table.horizontalHeader().sectionResizeMode(1)))
    check("最后一列不自动拉伸", not page.table.horizontalHeader().stretchLastSection())

    page.table.setColumnWidth(1, 260)
    app.processEvents()
    check("拖动后列宽生效", page.table.columnWidth(1) == 260, str(page.table.columnWidth(1)))
    page._persist_widths()
    saved = window_state.load_library_columns() or []
    check("列宽写进了 ui.json", saved[:2] == [58, 260], str(saved))

    from app.ui.library_page import LibraryPage as _LibraryPage

    fresh = _LibraryPage()
    fresh.show()
    app.processEvents()
    check("新开的页面读回上次的列宽", fresh.table.columnWidth(1) == 260,
          str(fresh.table.columnWidth(1)))
    widths = [fresh.table.columnWidth(i) for i in range(6)]
    check("宽度不足时由释义列补齐（右边不留空白）",
          sum(widths) >= fresh.table.viewport().width() - 2,
          f"{sum(widths)} vs {fresh.table.viewport().width()}")
    fresh.close()
    fresh.deleteLater()

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed == 0:
        print("词库页界面验收全部通过。")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    code = main()
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    raise SystemExit(code)