"""框选识别范围：服务层验收（不联网、不启动界面）。

运行：
    .\\venv\\Scripts\\python.exe scripts\\verify_crop.py

验证的是"框选区域真的被裁出来了"：用一张四分区、颜色分明的图，把每个角框一遍，
再解码送回模型的那段 base64，看像素对不对。界面部分见 scripts/verify_crop_ui.py。
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app import config, models
from app.services import entry_service, recognizer
from app.services.errors import ImageInvalid

WORK_DIR = config.DATA_DIR / "_verify_crop"

_passed = 0
_failed = 0

# 四分区：左上红、右上绿、左下蓝、右下白
RED = (220, 30, 30)
GREEN = (30, 160, 60)
BLUE = (30, 60, 200)
WHITE = (255, 255, 255)


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  [OK] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


def expect_error(name: str, exc_type: type[BaseException], func) -> BaseException | None:
    try:
        func()
    except exc_type as exc:
        check(name, True)
        return exc
    except Exception as exc:  # noqa: BLE001 - 报告类型不符
        check(name, False, f"抛出的是 {type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有抛出异常")
    return None


def make_quad_image(path: Path, size: int = 400) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (size, size), WHITE)
    half = size // 2
    image.paste(Image.new("RGB", (half, half), RED), (0, 0))
    image.paste(Image.new("RGB", (size - half, half), GREEN), (half, 0))
    image.paste(Image.new("RGB", (half, size - half), BLUE), (0, half))
    image.save(path, "PNG")
    return path


def decode(data_url: str) -> Image.Image:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGB")


def colour_of(image: Image.Image) -> tuple[int, int, int]:
    """取中间一小块的平均色，避免 JPEG 边缘噪声影响判断。"""
    width, height = image.size
    box = image.crop((width // 2 - 2, height // 2 - 2, width // 2 + 2, height // 2 + 2))
    pixels = list(box.getdata())
    count = len(pixels)
    return tuple(sum(channel) // count for channel in zip(*pixels))  # type: ignore[return-value]


def near(actual: tuple[int, int, int], expected: tuple[int, int, int], tolerance: int = 24) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


def main() -> int:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    image = make_quad_image(WORK_DIR / "quad.png")

    print("\n== 1. 不框选：整张图原样发出 ==")
    full = decode(recognizer.prepare_image(image))
    check("尺寸不变", full.size == (400, 400), str(full.size))
    check("四个角颜色都对", all(
        near(full.getpixel(point), expected)
        for point, expected in (
            ((100, 100), RED),
            ((300, 100), GREEN),
            ((100, 300), BLUE),
            ((300, 300), WHITE),
        )
    ))
    check("selection() 语义一致：None 表示整张", recognizer.normalize_crop(None) is None)

    print("\n== 2. 四个象限分别框一遍 ==")
    cases = [
        ("左上", (0.0, 0.0, 0.5, 0.5), RED),
        ("右上", (0.5, 0.0, 0.5, 0.5), GREEN),
        ("左下", (0.0, 0.5, 0.5, 0.5), BLUE),
        ("右下", (0.5, 0.5, 0.5, 0.5), WHITE),
    ]
    for name, crop, expected in cases:
        cropped = decode(recognizer.prepare_image(image, crop))
        check(f"{name}框出来是 200x200", cropped.size == (200, 200), str(cropped.size))
        check(f"{name}区域颜色是{expected}", near(colour_of(cropped), expected), str(colour_of(cropped)))

    print("\n== 3. 中间一小块也能精确裁 ==")
    centered = decode(recognizer.prepare_image(image, (0.25, 0.25, 0.5, 0.5)))
    check("尺寸 200x200", centered.size == (200, 200), str(centered.size))
    # 这个框正中对准四个色块的交点，所以四个角分别对应四种颜色
    check("左上角是红", near(centered.getpixel((40, 40)), RED), str(centered.getpixel((40, 40))))
    check("右上角是绿", near(centered.getpixel((160, 40)), GREEN), str(centered.getpixel((160, 40))))
    check("左下角是蓝", near(centered.getpixel((40, 160)), BLUE), str(centered.getpixel((40, 160))))
    check("右下角是白", near(centered.getpixel((160, 160)), WHITE), str(centered.getpixel((160, 160))))

    print("\n== 4. 越界与非法输入被收拾干净 ==")
    clamped = recognizer.normalize_crop((-0.3, -0.3, 0.5, 0.5))
    check("-0.3 被夹到 0", clamped[0] == 0.0 and clamped[1] == 0.0, str(clamped))
    over = recognizer.normalize_crop((0.8, 0.8, 0.5, 0.5))
    check("超出去的部分被夹掉", abs(over[2] - 0.2) < 1e-6 and abs(over[3] - 0.2) < 1e-6, str(over))
    check("框满整张 = (0,0,1,1)", recognizer.normalize_crop((0, 0, 1, 1)) == (0.0, 0.0, 1.0, 1.0))

    tiny = expect_error("太小的框报 ImageInvalid", ImageInvalid, lambda: recognizer.normalize_crop((0.5, 0.5, 0.001, 0.5)))
    if tiny is not None:
        check("提示里让人重框或还原", "还原整张" in tiny.user_message, tiny.user_message)
    expect_error("宽度为 0 也拦下", ImageInvalid, lambda: recognizer.normalize_crop((0.2, 0.2, 0.0, 0.5)))
    expect_error("NaN 拦下", ImageInvalid, lambda: recognizer.normalize_crop((float("nan"), 0, 1, 1)))
    expect_error("瞎传的字符串拦下", ImageInvalid, lambda: recognizer.normalize_crop(("a", "b", "c", "d")))  # type: ignore[arg-type]
    check("宽度为 0 的裁剪不会落到 prepare_image", True)

    print("\n== 5. 裁完仍然走原来的缩放规则 ==")
    big = Image.new("RGB", (4000, 2000), WHITE)
    big.paste(Image.new("RGB", (2000, 1000), RED), (0, 0))
    big_path = WORK_DIR / "big.png"
    big.save(big_path, "PNG")
    shrunk = decode(recognizer.prepare_image(big_path, (0, 0, 0.5, 0.5)))
    check("裁出 2000x1000 后按长边缩到 1600 以内", max(shrunk.size) <= recognizer.MAX_IMAGE_EDGE, str(shrunk.size))
    check("缩放后颜色没串", near(colour_of(shrunk), RED), str(colour_of(shrunk)))

    print("\n== 5.5 手机竖拍（EXIF 方向）：先摆正再裁 ==")
    rotated = WORK_DIR / "rotated.jpg"
    raw = Image.new("RGB", (400, 200), RED)
    raw.paste(Image.new("RGB", (200, 200), GREEN), (200, 0))  # 原始：左红右绿
    exif = raw.getexif()
    exif[274] = 6  # Orientation=6：顺时针转 90 度显示
    raw.save(rotated, "JPEG", exif=exif)

    upright = decode(recognizer.prepare_image(rotated))
    check("不框选时已摆正成 200x400", upright.size == (200, 400), str(upright.size))
    check("摆正后上半是红的（原来的左半边）", near(colour_of(upright.crop((0, 0, 200, 200))), RED))

    top = decode(recognizer.prepare_image(rotated, (0.0, 0.0, 1.0, 0.5)))
    check("裁上半得到 200x200", top.size == (200, 200), str(top.size))
    check("裁到的是红色那半（不是绿）", near(colour_of(top), RED), str(colour_of(top)))

    bottom = decode(recognizer.prepare_image(rotated, (0.0, 0.5, 1.0, 0.5)))
    check("裁下半得到绿色那半", near(colour_of(bottom), GREEN), str(colour_of(bottom)))

    print("\n== 6. crop 参数确实传到了识别服务 ==")
    captured: dict[str, object] = {}

    def fake_recognize(image_path, entry_type, *, crop=None, source_image="", timeout=None):
        captured["path"] = image_path
        captured["crop"] = crop
        captured["type"] = entry_type
        return models.Entry(type=entry_type, word="食べる")

    original = recognizer.recognize
    recognizer.recognize = fake_recognize  # type: ignore[assignment]
    try:
        entry_service.recognize_image(image, models.ENTRY_TYPE_WORD, crop=(0.1, 0.2, 0.3, 0.4))
        check("entry_service 透传 crop", captured.get("crop") == (0.1, 0.2, 0.3, 0.4), str(captured.get("crop")))
        entry_service.recognize_image(image, models.ENTRY_TYPE_WORD)
        check("不传 crop 时是 None", captured.get("crop") is None, str(captured.get("crop")))
    finally:
        recognizer.recognize = original  # type: ignore[assignment]

    print(f"\n===== 通过 {_passed} 项，失败 {_failed} 项 =====")
    if _failed == 0:
        print("框选识别范围（服务层）验收全部通过。界面请跑 scripts/verify_crop_ui.py。")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        import shutil

        shutil.rmtree(WORK_DIR, ignore_errors=True)
    raise SystemExit(code)