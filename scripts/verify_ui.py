"""阶段 3 验收脚本（界面部分）：在 offscreen 模式下真的把主窗口和录入页跑一遍。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_ui.py

它不联网、不弹窗，做的是"人点按钮"的等价操作：切类型、加载图片、填表单、点保存、
以及把识别服务临时替换成"必定失败/必定成功"，确认界面在两种情况下都不崩、提示正确。
用的是临时库与临时图片目录，跑完删除。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from PySide6.QtCore import QEventLoop, QSize, Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel

from app import config, models
from app.services import entry_service, enricher
from app.services.errors import ApiRequestFailed
from app.storage.db import init_db
from app.storage.repository import EntryRepository
from app.ui.capture_page import CapturePage
from app.ui.main_window import MainWindow
from app.ui import window_state

WORK_DIR = config.DATA_DIR / "_verify_ui"
UPLOAD_ROOT = WORK_DIR / "uploads"
VERIFY_DB = WORK_DIR / "ui.db"

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


def labels_of(widget) -> list[str]:
    return [child.text() for child in widget.findChildren(QLabel)]


def wait_worker(
    page: CapturePage,
    app: QApplication,
    timeout_ms: int = 8000,
    worker_getter=None,
) -> None:
    """等识别（或补全）线程结束，并让主线程把排队过来的信号处理掉。"""
    worker = worker_getter() if worker_getter is not None else page._worker
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    app.processEvents()


def make_jpeg(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 40), (30, 90, 200)).save(path, "JPEG")
    return path


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    # 把存储与图片目录都指到临时目录，绝不碰 data/app.db 和 data/uploads
    config.DATA_DIR = WORK_DIR
    config.UPLOAD_DIR = UPLOAD_ROOT
    config.DB_PATH = VERIFY_DB
    init_db(VERIFY_DB)
    repo = EntryRepository(VERIFY_DB)

    app = QApplication.instance() or QApplication([])
    print(f"临时库: {VERIFY_DB}")

    # ---------------------------------------------------------------- 1
    print("\n== 1. 主窗口与导航 ==")
    ready_settings = config.Settings(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        vision_model="test-model",
        upload_port=8765,
        reminder_time="21:00",
    )
    window = MainWindow(ready_settings)
    window.show()

    nav = window._nav
    stack = window._stack
    check("窗口标题含「录入」", "录入" in window.windowTitle(), window.windowTitle())
    check("左侧导航有 4 项", nav.count() == 4, str(nav.count()))
    check("页面容器有 4 页", stack.count() == 4, str(stack.count()))
    check("默认停在录入页", stack.currentIndex() == 0, str(stack.currentIndex()))

    nav.setCurrentRow(2)
    app.processEvents()
    check("点「打卡」能切到第 3 页", stack.currentIndex() == 2, str(stack.currentIndex()))
    check("标题跟着变", "打卡" in window.windowTitle(), window.windowTitle())
    check(
        "打卡页是真的页面（阶段 6 已接入）",
        stack.currentWidget() is window.checkin_page,
        type(stack.currentWidget()).__name__,
    )

    nav.setCurrentRow(0)
    app.processEvents()
    page = window.capture_page
    check("回到录入页", stack.currentWidget() is page)

    print("\n== 2. 配置缺失时的黄色提示条 ==")
    no_key_settings = config.Settings(
        api_key=None,
        base_url="https://example.invalid/v1",
        vision_model=None,
        upload_port=8765,
        reminder_time="21:00",
    )
    window_no_key = MainWindow(no_key_settings)
    texts = " ".join(labels_of(window_no_key))
    check("提示缺少 OPENAI_API_KEY", "OPENAI_API_KEY" in texts)
    check("提示缺少 VISION_MODEL", "VISION_MODEL" in texts)
    check("说明仍可手动录入", "手动录入" in texts)
    window_no_key.close()

    # ---------------------------------------------------------------- 3
    print("\n== 3. 类型切换 ==")
    check("默认是生词", page.current_type() == models.ENTRY_TYPE_WORD, page.current_type())
    check("生词：词形可编辑", page._editors["word"].isEnabled())
    check("生词：语法点被禁用", not page._editors["grammar_point"].isEnabled())
    check("生词：备注仍可编辑（两类共用）", page._editors["note"].isEnabled())

    page._grammar_radio.setChecked(True)
    app.processEvents()
    check("切到语法", page.current_type() == models.ENTRY_TYPE_GRAMMAR, page.current_type())
    check("语法：语法点可编辑", page._editors["grammar_point"].isEnabled())
    check("语法：词形被禁用", not page._editors["word"].isEnabled())
    check("语法：释义仍可编辑", page._editors["meaning"].isEnabled())
    check(
        "语法页有说明文字",
        "语法" in page._type_note.text(),
        page._type_note.text(),
    )
    page._word_radio.setChecked(True)
    app.processEvents()

    # ---------------------------------------------------------------- 4
    print("\n== 4. 选图 ==")
    jpeg = make_jpeg(WORK_DIR / "src" / "shot.jpg")
    page._load_image(str(jpeg))
    app.processEvents()
    check("预览有画面", page._preview.image_path is not None)
    check("「开始识别」被启用", page._recognize_button.isEnabled())
    check(
        "显示出图片路径",
        str(jpeg) in page._image_label.text(),
        page._image_label.text(),
    )
    check("给了「已选择」提示", "已选择" in page._banner.text(), page._banner.text())

    fake = WORK_DIR / "src" / "readme.jpg"
    fake.write_text("这不是图片", encoding="utf-8")
    page._load_image(str(fake))
    app.processEvents()
    check("坏图不崩，给出错误提示", "打不开" in page._banner.text(), page._banner.text())
    check("坏图时禁用识别", not page._recognize_button.isEnabled())

    # ---------------------------------------------------------------- 5
    print("\n== 5. 识别失败后仍可手填保存 ==")
    page._load_image(str(jpeg))
    app.processEvents()

    original_recognize = entry_service.recognize_and_enrich

    def boom(*_args, **_kwargs):
        raise ApiRequestFailed(401, "simulated: invalid api key")

    entry_service.recognize_and_enrich = boom
    page._start_recognition()
    wait_worker(page, app)
    entry_service.recognize_and_enrich = original_recognize

    check("红色错误提示出现了", "API Key" in page._banner.text(), page._banner.text())
    check("提示里给了退路", "手动" in page._banner.text(), page._banner.text())
    check("识别按钮恢复可用", page._recognize_button.isEnabled())
    check("保存按钮仍可用", page._save_button.isEnabled())
    check("表单仍可输入", page._editors["word"].isEnabled())

    page._editors["word"].setText("食べる")
    page._editors["reading"].setText("たべる")
    page._editors["meaning"].setText("吃")
    page._save()
    app.processEvents()
    check("手填的记录存进去了", repo.count() == 1, str(repo.count()))
    saved = repo.search(limit=1)[0]
    check("source_image 是相对路径", "/" in saved.source_image and ":" not in saved.source_image, saved.source_image)
    check(
        "图片在 uploads 下能找到",
        entry_service.absolute_image_path(saved.source_image, root=UPLOAD_ROOT) is not None,
        saved.source_image,
    )
    check("保存后给了成功提示", "已保存" in page._banner.text(), page._banner.text())
    check("保存后表单被清空", page._editors["word"].text() == "", page._editors["word"].text())
    # 改成保留图片：一页纸上常有好几条，保存完要能接着框下一块
    check("保存后图片还留着", page._image_path is not None, str(page._image_path))
    check("保存后提示里说了可以接着录", "接着录下一条" in page._banner.text(), page._banner.text())

    # ---------------------------------------------------------------- 6
    print("\n== 6. 识别成功后自动填表 ==")
    page._load_image(str(jpeg))
    app.processEvents()

    def fake_ok(_path, entry_type, crop=None, **_kwargs):
        return entry_service.RecognitionResult(
            entry=models.Entry(
                type=entry_type,
                word="走る",
                reading="はしる",
                meaning="跑",
                part_of_speech="动词・五段",
                example="毎朝走る。",
                example_cn="每天早上跑。",
            )
        )

    entry_service.recognize_and_enrich = fake_ok
    page._start_recognition()
    wait_worker(page, app)
    entry_service.recognize_and_enrich = original_recognize

    check("词形被填进去了", page._editors["word"].text() == "走る", page._editors["word"].text())
    check("读音被填进去了", page._editors["reading"].text() == "はしる")
    check("备注还空着（模型没给）", page._editors["note"].toPlainText() == "")
    check("提示为成功态", "识别完成" in page._banner.text(), page._banner.text())

    page._save()
    app.processEvents()
    check("识别结果也能保存", repo.count() == 2, str(repo.count()))
    check("两条都是生词", repo.count(entry_type=models.ENTRY_TYPE_WORD) == 2)

    # ---------------------------------------------------------------- 7
    print("\n== 7. AI 补的字段标记 ==")

    def fake_enriched(_path, entry_type, crop=None, **_kwargs):
        return entry_service.RecognitionResult(
            entry=models.Entry(
                type=entry_type,
                word="取り組み",
                reading="とりくみ",
                meaning="努力；举措",
                part_of_speech="名词",
                example="環境問題への取り組みが進んでいる。",
                example_cn="应对环境问题的举措正在推进。",
                generated_fields="meaning,example,example_cn",
            )
        )

    entry_service.recognize_and_enrich = fake_enriched
    page._start_recognition()
    wait_worker(page, app)
    entry_service.recognize_and_enrich = original_recognize

    check("AI 补的字段被标出来了", "AI 补" in page._field_labels["meaning"].text(),
          page._field_labels["meaning"].text())
    check("AI 补的字段有浅蓝底", "#eef4fb" in page._editors["meaning"].styleSheet(),
          page._editors["meaning"].styleSheet())
    check("照抄的字段没被标", "AI 补" not in page._field_labels["word"].text(),
          page._field_labels["word"].text())
    check("汇总提示出现了", page._ai_hint.isVisible() and "3 个字段" in page._ai_hint.text(),
          page._ai_hint.text())

    page._save()
    app.processEvents()
    marked = repo.search(limit=1)[0]
    check("AI 标记跟着记录存进库",
          marked.generated_fields == "meaning,example,example_cn", marked.generated_fields)
    check("保存后标记被清掉",
          page._ai_hint.isHidden() and "AI 补" not in page._field_labels["meaning"].text())

    # ---------------------------------------------------------------- 8
    print("\n== 8. 自动补全开关与「重新补全」==")
    check("开关默认是开着的", page._auto_enrich_check.isChecked())
    page._auto_enrich_check.setChecked(False)
    app.processEvents()
    check("关掉后写进 ui.json", window_state.load_auto_enrich() is False)
    page._auto_enrich_check.setChecked(True)
    app.processEvents()
    check("再打开也写进 ui.json", window_state.load_auto_enrich() is True)

    check("主字段空时不能补全", not page._reenrich_button.isEnabled())
    page._editors["word"].setText("取り組み")
    app.processEvents()
    check("填了主字段就能补全", page._reenrich_button.isEnabled())

    original_enrich = entry_service.enrich_entry

    def fake_enrich(entry, *_args, **_kwargs):
        enriched = models.Entry(**entry.__dict__)
        enriched.meaning = "努力；举措"
        enriched.generated_fields = "meaning"
        return entry_service.RecognitionResult(
            entry=enriched,
            enrichment=enricher.Enrichment(
                fields={"meaning": "努力；举措"},
                rejected={},
                raw={},
                elapsed=0.4,
                model="fake",
            ),
        )

    entry_service.enrich_entry = fake_enrich
    page._start_reenrich()
    wait_worker(page, app, worker_getter=lambda: page._enrich_worker)
    entry_service.enrich_entry = original_enrich

    check("补全结果填进了表单", page._editors["meaning"].text() == "努力；举措",
          page._editors["meaning"].text())
    check("补全的字段也被标出来", "AI 补" in page._field_labels["meaning"].text())
    check("补全后给了成功提示", "补全完成" in page._banner.text(), page._banner.text())

    # ---------------------------------------------------------------- 9
    print("\n== 9. 表单校验 ==")
    page._load_image(str(jpeg))
    app.processEvents()
    page._editors["word"].setText("   ")
    page._save()
    app.processEvents()
    check("空词形被拦下", repo.count() == 3, str(repo.count()))
    check("提示指出缺词形", "词形" in page._banner.text(), page._banner.text())

    page._editors["word"].setText("  静か  ")
    page._save()
    app.processEvents()
    latest = repo.search(limit=1)[0]
    check("保存时去掉了首尾空格", latest.word == "静か", latest.word)

    # ---------------------------------------------------------------- 10
    print("\n== 10. 窗口底座 ==")
    from app.ui import style

    check("窗口有最小尺寸", window.minimumSize() == QSize(*style.MIN_WINDOW_SIZE),
          str(window.minimumSize()))
    check("导航宽度取自 style", window._nav.width() == style.NAV_WIDTH, str(window._nav.width()))

    # 缩到最小尺寸，各块不该被压成 0 宽
    window.resize(*style.MIN_WINDOW_SIZE)
    app.processEvents()
    check("最小尺寸下预览还有宽度", page._preview.width() > 0, str(page._preview.width()))
    check("最小尺寸下表单还有宽度", page._editors["word"].width() > 0,
          str(page._editors["word"].width()))
    check("最小尺寸下保存按钮没被挤没", page._save_button.width() >= 100,
          str(page._save_button.width()))
    window.resize(1400, 900)
    app.processEvents()
    check("放大后预览跟着变大或至少不变", page._preview.width() >= 1, str(page._preview.width()))
    check("放大后预览确实比最小尺寸宽", page._preview.width() > 300, str(page._preview.width()))
    check("录入页左右两栏可拖动", page._body_splitter.orientation() == Qt.Horizontal,
          str(page._body_splitter.orientation()))
    check("备注框不会无限长高", page._editors["note"].height() <= style.NOTE_MAX_HEIGHT,
          str(page._editors["note"].height()))
    check("保存按钮用主色样式", "#2f6f9f" in page._save_button.styleSheet(),
          page._save_button.styleSheet()[:40])
    check("按钮最小高度取自 style",
          page._recognize_button.minimumHeight() == style.BUTTON_HEIGHT,
          str(page._recognize_button.minimumHeight()))

    # 关窗口时把尺寸记下来，下次启动恢复
    window.close()
    app.processEvents()
    saved_state = window_state.load_window_state()
    check("关窗口写下了窗口尺寸", saved_state.get("width") == 1400,
          str(saved_state))
    check("记下的高度也对", saved_state.get("height") == 900, str(saved_state))

    # ---------------------------------------------------------------- 收尾
    shutil.rmtree(WORK_DIR, ignore_errors=True)

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed == 0:
        print("录入页（界面部分）验收全部通过。")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())