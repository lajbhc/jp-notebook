"""录入相关的业务门面。

UI 只跟本模块打交道：不直接调 recognizer、不直接碰 repository、也不自己拼路径。
这样做的好处是"选图 → 识别 → 校正 → 落库"这条流程只有一个实现，阶段 5 的手机
上传服务要复用同一条流程时，直接调这里就行，不用再写一遍。

本模块犯的错分两类，对 UI 的约定不同：
- `RecognitionError` 子类：识别链路的问题，带 `user_message` 与 `detail`。
- `EntryValidationError`：表单内容不合法（比如词形空着），只带 `user_message`。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from app import config, models
from app.logger import get_logger
from app.services import enricher, recognizer
from app.services.errors import ImageInvalid, RecognitionError
from app.storage.db import StorageError
from app.storage.repository import EntryRepository

log = get_logger("services.entry")

# Pillow 的 format 名 -> 落盘后缀。只接受这些格式，和 recognizer 的白名单保持一致。
_FORMAT_SUFFIX: dict[str, str] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "GIF": ".gif",
    "TIFF": ".tif",
}

# 两类记录各自"不留就报错"的字段
_REQUIRED_TEXT: dict[str, str] = {
    models.ENTRY_TYPE_WORD: "word",
    models.ENTRY_TYPE_GRAMMAR: "grammar_point",
}

# 类型切换时要清掉的另一类字段，避免一条语法记录里残留着生词的词形
_EXCLUSIVE_GROUPS: dict[str, tuple[str, ...]] = {
    models.ENTRY_TYPE_WORD: ("grammar_point", "connection"),
    models.ENTRY_TYPE_GRAMMAR: ("word", "reading", "part_of_speech"),
}

_TEXT_FIELDS: tuple[str, ...] = (
    "word",
    "reading",
    "meaning",
    "part_of_speech",
    "grammar_point",
    "connection",
    "example",
    "example_cn",
    "error_reason",
    "note",
)


class EntryValidationError(ValueError):
    """表单内容不合法。UI 直接显示 user_message，不需要日志。"""

    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)


class SaveFailed(RuntimeError):
    """写库失败（磁盘满、库文件被占用、表结构版本不对等）。

    在这里把 storage 的异常包一层，是为了让 UI 只 import services 这一层：
    如果让界面直接 catch `StorageError`，它就不得不 import storage，
    分层方向就破了。
    """

    def __init__(self, user_message: str, detail: str = "") -> None:
        self.user_message = user_message
        self.detail = detail
        super().__init__(detail or user_message)


@dataclass(frozen=True)
class RecognitionResult:
    """识别（外加补全）的结果。

    为什么不是直接返回 Entry：补全是"加分项"，它失败时识别结果照样有用，
    所以要把"补全没成功"当成结果的一部分返回，而不是抛异常让界面丢掉整条记录。
    """

    entry: models.Entry
    enrichment: enricher.Enrichment | None = None
    enrich_error: str = ""

    @property
    def enriched(self) -> bool:
        return bool(self.enrichment and self.enrichment.fields)

    @property
    def note(self) -> str:
        """给界面显示的一句话（可以空着）。"""
        if self.enrich_error:
            return f"补全没成功：{self.enrich_error}（可以手动填写）"
        if self.enrichment is not None:
            return self.enrichment.summary
        return ""


# --------------------------------------------------------------- 图片
def upload_root() -> Path:
    """图片存放根目录。测试时可以传参覆盖，生产走 config。"""
    return config.UPLOAD_DIR


def store_image(
    source: str | Path,
    *,
    root: Path | None = None,
    today: str | None = None,
) -> str:
    """把选中的图片复制进 data/uploads/<日期>/，返回相对路径（如 2026-11-01/ab12cd.jpg）。

    为什么复制而不是只记原路径：原图可能被删掉、被移动，或者在手机/相机目录里。
    复制进 data/ 之后这条记录就自包含了，翻看历史时不会出现"图片找不到"。
    数据库只存相对路径，换机器/换目录时不用改库。
    """
    path = Path(source)
    if not path.is_file():
        raise ImageInvalid(
            f"找不到原图: {path}",
            "原图不见了（可能被移动或删除），请重新选择图片。",
        )

    try:
        with Image.open(path) as image:
            image_format = (image.format or "").upper()
            image.verify()  # 只校验完整性，不读像素
    except UnidentifiedImageError as exc:
        raise ImageInvalid(
            f"not a known image: {path}",
            "这个文件不是可识别的图片。iPhone 的 HEIC 格式不支持，"
            "请用微信/QQ 传图，或先转成 JPG。",
        ) from exc
    except OSError as exc:
        raise ImageInvalid(
            f"cannot open image: {path}: {exc}",
            "这张图片打不开，可能已损坏，请换一张试试。",
        ) from exc

    suffix = _FORMAT_SUFFIX.get(image_format)
    if suffix is None:
        raise ImageInvalid(
            f"unsupported format {image_format!r}: {path}",
            f"不支持 {image_format or '这种'} 格式，请换成 JPG / PNG。",
        )

    day = today or date.today().isoformat()
    folder = (root or upload_root()) / day
    folder.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid4().hex[:12]}{suffix}"
    try:
        shutil.copy2(path, folder / filename)
    except OSError as exc:
        raise ImageInvalid(
            f"cannot copy {path} -> {folder}: {exc}",
            "图片保存失败，请检查磁盘空间或 data 目录权限。",
        ) from exc

    # 统一用正斜杠，Windows 与将来的其他平台读出来都一样
    return f"{day}/{filename}"


def absolute_image_path(relative: str, *, root: Path | None = None) -> Path | None:
    """相对路径 -> 绝对路径。空值或文件已不存在时返回 None，让界面自己决定怎么显示。"""
    if not relative:
        return None
    path = (root or upload_root()) / relative
    return path if path.is_file() else None


# --------------------------------------------------------------- 识别
def recognize_image(
    image_path: str | Path,
    entry_type: str,
    crop: tuple[float, float, float, float] | None = None,
) -> models.Entry:
    """调识别服务。失败时抛 RecognitionError 子类，由 UI 统一提示。

    `crop` 是录入页上框出来的区域（归一化 `(x, y, w, h)`），None 表示整张。
    """
    return recognizer.recognize(image_path, entry_type, crop=crop)


def recognize_and_enrich(
    image_path: str | Path,
    entry_type: str,
    *,
    crop: tuple[float, float, float, float] | None = None,
    source_image: str = "",
    auto_enrich: bool = True,
) -> RecognitionResult:
    """识别一张图，然后把空字段补上。

    补全失败**不会**让整次识别失败：返回的 `RecognitionResult` 里 `enrich_error`
    带一句人话，`entry` 还是可以校正保存的。原图里的内容永远优先，补全只填空。
    """
    entry = recognizer.recognize(
        image_path, entry_type, crop=crop, source_image=source_image
    )
    if not auto_enrich:
        return RecognitionResult(entry=entry)
    return enrich_entry(entry)


def enrich_entry(
    entry: models.Entry,
    *,
    targets: tuple[str, ...] | None = None,
) -> RecognitionResult:
    """给一条已有记录补空字段（录入页的「重新补全」和编辑旧记录也走这里）。"""
    if not enricher.missing_fields(entry):
        return RecognitionResult(entry=entry)

    try:
        result = enricher.enrich(entry, targets=targets)
    except RecognitionError as exc:
        log.warning("补全失败：%s", exc.detail or exc.user_message)
        return RecognitionResult(entry=entry, enrich_error=exc.user_message)
    except Exception as exc:  # noqa: BLE001 - 补全是加分项，不许它弄丢识别结果
        log.exception("补全时出现未预期的错误：%s", exc)
        return RecognitionResult(entry=entry, enrich_error="补全时出了点问题，可以手动填写。")

    return RecognitionResult(entry=enricher.apply(entry, result), enrichment=result)


# --------------------------------------------------------------- 保存
def new_entry(entry_type: str = models.ENTRY_TYPE_WORD) -> models.Entry:
    """建一条空记录，供表单绑定。"""
    if entry_type not in models.ENTRY_TYPES:
        raise ValueError(f"unknown entry_type: {entry_type!r}")
    return models.Entry(type=entry_type)


def _clean_generated_fields(text: str, entry: models.Entry) -> str:
    """整理"哪些字段是补的"：只留当前类型能补的、且现在还有内容的字段。

    为什么要过滤：用户可能把 AI 补的例句删掉了，那就不该继续标"这是 AI 补的"；
    也可能把一条生词切成语法，那时 grammar 专属的标记才有意义。
    """
    allowed = enricher.FILLABLE_FIELDS.get(entry.type, ())
    names: list[str] = []
    for raw in (text or "").split(","):
        name = raw.strip()
        if name in allowed and name not in names and getattr(entry, name, "").strip():
            names.append(name)
    return ",".join(names)


def normalize(entry: models.Entry) -> models.Entry:
    """校验并整理表单内容，返回可以直接落库的副本（不修改传入对象）。

    - 类型必须合法
    - 所有文本字段去首尾空白
    - 清掉另一类记录才用得到的字段
    - 主字段（生词的 word / 语法的 grammar_point）不能为空
    - "哪些字段是 AI 补的"跟着一起清理（见 `_clean_generated_fields`）
    """
    if entry.type not in models.ENTRY_TYPES:
        raise EntryValidationError("记录类型只能是「生词」或「语法」。")

    cleaned = models.Entry(id=entry.id, type=entry.type, source_image=entry.source_image)
    for field in _TEXT_FIELDS:
        setattr(cleaned, field, str(getattr(entry, field, "") or "").strip())

    for field in _EXCLUSIVE_GROUPS[entry.type]:
        setattr(cleaned, field, "")

    cleaned.generated_fields = _clean_generated_fields(entry.generated_fields, cleaned)

    required = _REQUIRED_TEXT[entry.type]
    if not getattr(cleaned, required):
        label = "词形" if required == "word" else "语法点"
        raise EntryValidationError(f"请先填写{label}，这是这条记录的主字段。")

    return cleaned


def save_entry(
    entry: models.Entry,
    *,
    image_source: str | Path | None = None,
    repo: EntryRepository | None = None,
    root: Path | None = None,
) -> models.Entry:
    """保存一条记录：新记录入库、已有记录按 id 更新。

    image_source 传了就把那张图复制进 uploads 并写入 source_image；不传则沿用
    entry 上已有的 source_image（编辑旧记录时就是这种情况）。
    """
    repository = repo or EntryRepository()

    # 先校验再复制图片：校验没过就不该在 uploads 里留下一张没人引用的图
    cleaned = normalize(entry)

    if image_source is not None:
        cleaned.source_image = store_image(image_source, root=root)

    try:
        if cleaned.id is None:
            return repository.create(cleaned)
        return repository.update(cleaned)
    except (StorageError, OSError) as exc:
        # OSError 也一起兜住：库文件所在目录建不出来（比如路径被一个文件占了）
        # 时抛的是 NotADirectoryError，它不是 StorageError。
        raise SaveFailed(
            "写数据库失败，请检查 data 目录是否可写、磁盘是否还有空间。",
            str(exc),
        ) from exc