"""框选识别范围：界面验收（offscreen 模式，不弹窗、不联网）。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_crop_ui.py

做的是"人拖鼠标框一块"的等价操作：直接给 `CropView` 送 QMouseEvent，
检查它算出来的归一化比例、letterbox 映射、误触过滤，以及录入页有没有把
这块框真的交给识别线程。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from PySide6.QtCore import QEvent, QEventLoop, QPointF, QSize, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog

from app import config, models
from app.services import entry_service
from app.storage.db import init_db
from app.storage.repository import EntryRepository
from app.ui import capture_page as capture_page_module
from app.ui.capture_page import CapturePage
from app.ui.crop_dialog import CropDialog
from app.ui.crop_view import CropView

WORK_DIR = config.DATA_DIR / "_verify_crop_ui"
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


def close_to(value: float, expected: float, tolerance: float = 0.01) -> bool:
    return abs(value - expected) <= tolerance


def make_png(path: Path, size: tuple[int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (30, 90, 200)).save(path, "PNG")
    return path


# --------------------------------------------------------------- 造鼠标事件
def _event(kind: QEvent.Type, x: float, y: float, button: Qt.MouseButton, buttons: Qt.MouseButton):
    point = QPointF(x, y)
    return QMouseEvent(kind, point, point, button, buttons, Qt.NoModifier)


def drag(view: CropView, start: tuple[float, float], end: tuple[float, float]) -> None:
    view.mousePressEvent(_event(QEvent.MouseButtonPress, *start, Qt.LeftButton, Qt.LeftButton))
    view.mouseMoveEvent(_event(QEvent.MouseMove, *end, Qt.NoButton, Qt.LeftButton))
    view.mouseReleaseEvent(_event(QEvent.MouseButtonRelease, *end, Qt.LeftButton, Qt.NoButton))


def right_click(view: CropView, x: float, y: float) -> None:
    view.mousePressEvent(_event(QEvent.MouseButtonPress, x, y, Qt.RightButton, Qt.RightButton))


def press_escape(view: CropView) -> None:
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))


def wait_worker(page: CapturePage, app: QApplication, timeout_ms: int = 8000) -> None:
    worker = page._worker
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    app.processEvents()


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    config.DATA_DIR = WORK_DIR
    config.UPLOAD_DIR = UPLOAD_ROOT
    config.DB_PATH = VERIFY_DB
    init_db(VERIFY_DB)
    repo = EntryRepository(VERIFY_DB)

    app = QApplication.instance() or QApplication([])

    # ---------------------------------------------------------------- 1
    print("\n== 1. 空状态 ==")
    view = CropView("还没有选择图片")
    view.resize(QSize(300, 220))
    check("没有图时 image_path 是 None", view.image_path is None)
    check("没有框选时 has_selection 是 False", not view.has_selection())
    check("没有框选时 selection() 是 None", view.selection() is None)
    check("没有框选时文案为空", view.selection_text() == "")
    check("拖一下也不会崩（没有图）", _drag_without_image(view))

    # ---------------------------------------------------------------- 2
    print("\n== 2. 正方形图：letterbox 映射 ==")
    square = make_png(WORK_DIR / "square.png", (400, 400))
    check("载入成功", view.set_image(square))
    check("图上没有残留框选", view.selection() is None)
    # 400x400 放进 300x220 的框里，缩放成 220x220，水平居中 -> 左边距 40
    check("图片区域是 220x220", view._image_rect.size() == QSize(220, 220), str(view._image_rect))
    check("水平居中（左边距 40）", view._image_rect.left() == 40, str(view._image_rect.left()))

    drag(view, (40, 0), (150, 110))
    box = view.selection()
    check("拖完有框选", box is not None)
    if box is not None:
        check("左上角换算成 0,0", close_to(box[0], 0.0) and close_to(box[1], 0.0), str(box))
        check("宽高约 50%", close_to(box[2], 0.5) and close_to(box[3], 0.5), str(box))
    check("文案显示宽高百分比", "宽 50%" in view.selection_text() and "高 50%" in view.selection_text(), view.selection_text())

    # ---------------------------------------------------------------- 3
    print("\n== 3. 拖到图外会被夹回来 ==")
    view.clear_selection()
    drag(view, (0, 0), (299, 219))  # 起点在 letterbox 空白处、终点也在
    box = view.selection()
    check("框不会超出图片区域", box is not None and close_to(box[0], 0.0) and close_to(box[1], 0.0), str(box))
    if box is not None:
        check("右下被夹在图内", close_to(box[2], 1.0, 0.02) and close_to(box[3], 1.0, 0.02), str(box))

    # ---------------------------------------------------------------- 4
    print("\n== 4. 误触：太小的框不算 ==")
    view.clear_selection()
    drag(view, (100, 100), (103, 101))
    check("3 像素的框被丢弃", view.selection() is None, str(view.selection()))
    check("框选状态回到 False", not view.has_selection())

    # ---------------------------------------------------------------- 5
    print("\n== 5. 取消方式：右键 / Esc / 清空图片 ==")
    drag(view, (40, 0), (150, 110))
    check("先框上一块", view.has_selection())
    right_click(view, 60, 60)
    check("右键取消框选", not view.has_selection())

    drag(view, (40, 0), (150, 110))
    press_escape(view)
    check("Esc 取消框选", not view.has_selection())

    drag(view, (40, 0), (150, 110))
    view.clear_image()
    check("清空图片时框选一起清掉", not view.has_selection() and view.image_path is None)

    # ---------------------------------------------------------------- 6
    print("\n== 6. 长方形图：上下留白，映射仍然准 ==")
    wide = make_png(WORK_DIR / "wide.png", (400, 200))
    check("载入长方形图", view.set_image(wide))
    rect = view._image_rect
    check("缩放成 300x150", rect.size() == QSize(300, 150), str(rect.size()))
    check("垂直居中（上边距 35）", rect.top() == 35, str(rect.top()))
    drag(view, (0, 35), (150, 110))
    box = view.selection()
    check("左上角是 0,0", box is not None and close_to(box[0], 0.0) and close_to(box[1], 0.0), str(box))
    if box is not None:
        check("宽 50%、高 50%", close_to(box[2], 0.5) and close_to(box[3], 0.5), str(box))

    # ---------------------------------------------------------------- 7
    print("\n== 7. 打不开的文件 ==")
    bad = WORK_DIR / "bad.png"
    bad.write_bytes(b"not an image")
    check("坏图返回 False", not view.set_image(bad))
    check("坏图后 image_path 被清空", view.image_path is None)
    check("坏图后框选也没了", not view.has_selection())

    # ---------------------------------------------------------------- 7.5
    print("\n== 7.5 手机竖拍（EXIF 方向）：预览必须和服务层摆成一个方向 ==")
    rotated = WORK_DIR / "rotated.jpg"
    raw = Image.new("RGB", (400, 200), (220, 30, 30))
    raw.paste(Image.new("RGB", (200, 200), (30, 160, 60)), (200, 0))  # 左红右绿
    exif = raw.getexif()
    exif[274] = 6  # Orientation=6：顺时针转 90 度显示
    raw.save(rotated, "JPEG", exif=exif)

    from PIL import ImageOps

    with Image.open(rotated) as opened:
        upright = ImageOps.exif_transpose(opened)
        check(
            "服务层看到的是竖的 200x400",
            upright.size == (200, 400),
            str(upright.size),
        )

    check("界面载入成功", view.set_image(rotated))
    check(
        "预览也按 EXIF 摆正成竖图（而不是 400x200 横着）",
        view._pixmap is not None and view._pixmap.size() == QSize(200, 400),
        str(view._pixmap.size() if view._pixmap else None),
    )
    drag(view, (40, 0), (150, 110))
    box = view.selection()
    check("摆正后框选比例正常", box is not None and close_to(box[2], 0.5), str(box))

    # ---------------------------------------------------------------- 8
    print("\n== 8. 录入页：框选交给识别线程 ==")
    view.deleteLater()
    page = CapturePage()
    page.show()
    app.processEvents()

    jpeg = make_png(WORK_DIR / "shot.png", (400, 400))
    page._load_image(str(jpeg))
    app.processEvents()
    check("录入页换成了 CropView", isinstance(page._preview, CropView))
    check("刚选完图时按钮不可用", not page._clear_crop_button.isEnabled())
    check("刚选完图时没有框选文案", page._crop_label.text() == "")

    drag(page._preview, (40, 0), (150, 110))
    app.processEvents()
    check("框选后按钮变可用", page._clear_crop_button.isEnabled())
    check("框选后文案出现", "只识别框选区域" in page._crop_label.text(), page._crop_label.text())

    captured: dict[str, object] = {}

    def fake_ok(path, entry_type, crop=None):
        captured["crop"] = crop
        captured["type"] = entry_type
        return models.Entry(type=entry_type, word="走る", reading="はしる", meaning="跑")

    original = entry_service.recognize_image
    entry_service.recognize_image = fake_ok
    try:
        expected = page._preview.selection()
        page._start_recognition()
        busy_text = page._banner.text()
        wait_worker(page, app)
        check("识别中提示说的是「框选的那一块」", "框选的那一块" in busy_text, busy_text)
        check("crop 被递给识别服务", captured.get("crop") == expected, f"{captured.get('crop')} != {expected}")
        check("识别结果照常填表", page._editors["word"].text() == "走る", page._editors["word"].text())

        page._clear_crop_button.click()
        app.processEvents()
        check("点「还原整张」后框选消失", not page._preview.has_selection())
        check("点「还原整张」后按钮变回不可用", not page._clear_crop_button.isEnabled())
        check("点「还原整张」后文案清空", page._crop_label.text() == "")

        page._start_recognition()
        whole_text = page._banner.text()
        wait_worker(page, app)
        check("识别中提示说的是「整张图」", "整张图" in whole_text, whole_text)
        check("没有框选时 crop 是 None", captured.get("crop") is None, str(captured.get("crop")))

        # 切类型不该把已经框好的区域弄丢（框的是图片，不是字段）
        drag(page._preview, (40, 0), (150, 110))
        page._grammar_radio.setChecked(True)
        app.processEvents()
        check("切到语法后框选还在", page._preview.has_selection())
        check("类型也切对了", page.current_type() == models.ENTRY_TYPE_GRAMMAR, page.current_type())

        page._save()
        app.processEvents()
        check("语法记录保存失败时被拦下（主字段还空着）", repo.count() == 0, str(repo.count()))

        page._editors["grammar_point"].setText("〜ことになる")
        page._editors["meaning"].setText("客观上决定")
        page._save()
        app.processEvents()
        check("补上语法点后保存成功", repo.count(entry_type=models.ENTRY_TYPE_GRAMMAR) == 1, str(repo.count()))
    finally:
        entry_service.recognize_image = original

    # ---------------------------------------------------------------- 9
    print("\n== 9. 重置表单时框选一起清掉 ==")
    # 「清空重来」是"连图一起清"，而保存成功后只清框、图留着，所以这里单独重新选一张
    page._load_image(str(jpeg))
    app.processEvents()
    drag(page._preview, (40, 0), (150, 110))
    check("先框一块", page._preview.has_selection())
    page._reset_form()
    app.processEvents()
    check("重置后框选没了", not page._preview.has_selection())
    check("重置后按钮不可用", not page._clear_crop_button.isEnabled())

    # ---------------------------------------------------------------- 10
    print("\n== 10. 放大框选窗口：框选比例在两个窗口之间来回传 ==")
    dialog = CropDialog(str(jpeg), None)
    check("窗口里有图", dialog._view.image_path is not None)
    check("没框选时「用这块识别」不可用", not dialog._use_button.isEnabled())
    check("提示让用户先框一块", "还没框选" in dialog._info.text(), dialog._info.text())
    check("放大窗口比小预览大得多", dialog._view.width() > page._preview.width(), str(dialog._view.size()))

    dialog._view.set_selection((0.1, 0.2, 0.3, 0.4))
    box = dialog.selection()
    check("框完按钮变可用", dialog._use_button.isEnabled())
    check(
        "框选比例原样读出来",
        box is not None and close_to(box[0], 0.1) and close_to(box[1], 0.2)
        and close_to(box[2], 0.3) and close_to(box[3], 0.4),
        str(box),
    )
    check("提示里报了尺寸", "30%" in dialog._info.text(), dialog._info.text())

    dialog._whole_button.click()
    check("「识别整张」把框清掉", dialog.selection() is None)
    check("「识别整张」关掉窗口", dialog.result() == QDialog.Accepted, str(dialog.result()))
    dialog.deleteLater()

    # ---------------------------------------------------------------- 10.5
    print("\n== 10.5 双击 / 按钮都能打开放大框选，结果回写小预览 ==")
    page._load_image(str(jpeg))
    app.processEvents()
    opened: dict[str, object] = {}

    class FakeDialog:
        """顶掉真窗口：只验录入页有没有正确调用、正确回写。"""

        selection_result = (0.2, 0.25, 0.4, 0.3)
        result_code = QDialog.Accepted

        def __init__(self, image_path, selection, parent=None):
            opened["path"] = str(image_path)
            opened["selection"] = selection

        def exec(self):
            return self.result_code

        def selection(self):
            return self.selection_result if self.result_code == QDialog.Accepted else None

        def deleteLater(self):
            pass

    original_dialog = capture_page_module.CropDialog
    capture_page_module.CropDialog = FakeDialog
    try:
        page._zoom_button.click()
        app.processEvents()
        check(
            "把当前图片传给了放大窗口",
            str(opened.get("path", "")).endswith("shot.png"),
            str(opened.get("path")),
        )
        box = page._preview.selection()
        check("结果回写到小预览", box is not None and close_to(box[2], 0.4), str(box))
        check("回写后按钮可用", page._clear_crop_button.isEnabled())
        check("回写后文案更新", "只识别框选区域" in page._crop_label.text(), page._crop_label.text())

        FakeDialog.result_code = QDialog.Rejected
        page._zoom_button.click()
        app.processEvents()
        box = page._preview.selection()
        check("取消后原来的框没被改掉", box is not None and close_to(box[2], 0.4), str(box))

        FakeDialog.result_code = QDialog.Accepted
        opened.clear()
        page._preview.double_clicked.emit()
        app.processEvents()
        check("双击图片也走同一个入口", "path" in opened)

        drag(page._preview, (10, 10), (60, 60))
        page._preview.mouseDoubleClickEvent(
            _event(QEvent.MouseButtonDblClick, 30, 30, Qt.LeftButton, Qt.LeftButton)
        )
        app.processEvents()
        check("真的双击事件会发信号", "path" in opened)
    finally:
        capture_page_module.CropDialog = original_dialog

    # ---------------------------------------------------------------- 11
    print("\n== 11. 同一张图接着录下一条 ==")
    page._word_radio.setChecked(True)  # 前面切成语法了，这里回到生词
    app.processEvents()
    page._load_image(str(jpeg))
    app.processEvents()
    drag(page._preview, (40, 0), (150, 110))
    page._editors["word"].setText("走る")
    page._editors["meaning"].setText("跑")
    before = repo.count()
    page._save()
    app.processEvents()

    check("第一条存进去了", repo.count() == before + 1, str(repo.count()))
    first = repo.search(limit=1)[0]
    check("保存后图片还在预览里", page._image_path is not None, str(page._image_path))
    check("保存后框选被清掉（好拖下一个框）", not page._preview.has_selection())
    check("保存后识别按钮可用", page._recognize_button.isEnabled())
    check("保存后表单清空了", page._editors["word"].text() == "", page._editors["word"].text())
    check("提示里说了可以接着录", "接着录下一条" in page._banner.text(), page._banner.text())
    check(
        "记住了 uploads 里的相对路径",
        page._image_rel == first.source_image,
        f"{page._image_rel} != {first.source_image}",
    )

    day_dir = UPLOAD_ROOT / Path(first.source_image).parent
    files_before = len(list(day_dir.glob("*")))

    drag(page._preview, (40, 0), (150, 110))
    page._editors["word"].setText("走ります")
    page._editors["meaning"].setText("跑（敬体）")
    page._save()
    app.processEvents()

    check("第二条也存进去了", repo.count() == before + 2, str(repo.count()))
    second = repo.search(limit=1)[0]
    check(
        "两条记的是同一张原图",
        second.source_image == first.source_image,
        f"{second.source_image} != {first.source_image}",
    )
    check(
        "没有把同一张图重复复制",
        len(list(day_dir.glob("*"))) == files_before,
        f"{len(list(day_dir.glob('*')))} != {files_before}",
    )

    page._reset_form()
    app.processEvents()
    check("「清空重来」把图也清掉了", page._image_path is None and page._preview.image_path is None)
    check("「清空重来」后忘了的相对路径也没了", page._image_rel is None)

    page.close()
    app.processEvents()
    page.deleteLater()

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed == 0:
        print("框选识别范围（界面）验收全部通过。")
    return 0 if _failed == 0 else 1


def _drag_without_image(view: CropView) -> bool:
    """没有图片时拖鼠标应该什么都不发生，也不能抛异常。"""
    try:
        drag(view, (10, 10), (120, 90))
    except Exception:  # noqa: BLE001 - 这里就是要确保不抛
        return False
    return view.selection() is None


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    raise SystemExit(code)