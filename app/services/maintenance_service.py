"""图片目录的整理：谁没被引用、谁在重复、清掉孤儿。

只有两件事：**看**（`scan`）和**删孤儿**（`delete_orphans`）。

为什么不自动删：删记录时故意保留原图（用户可能只是想重录），所以"没被引用"
不等于"当初不该留"。真正决定删不删的是人，这里只负责把账算清楚。

删除前一定重新查一遍数据库——界面上那份清单可能是几秒前的，这期间完全可能
又录了一条引用同一张图的记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.logger import get_logger
from app.services import entry_service
from app.storage.db import StorageError
from app.storage.repository import EntryRepository

log = get_logger("services.maintenance")

# data/uploads 下的下划线目录是临时中转（手机上传先落这里再转正），不算正式图片
SKIP_DIRS = {"_incoming"}


class MaintenanceFailed(RuntimeError):
    """整理过程出错。UI 只认识这一个异常，显示 `user_message`。"""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail


def size_text(size: int) -> str:
    """把字节数说成人话：822 B / 12.3 KB / 4.1 MB。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@dataclass(frozen=True)
class ImageFile:
    relative_path: str
    size: int

    @property
    def size_text(self) -> str:
        return size_text(self.size)


@dataclass(frozen=True)
class ImageReport:
    files: tuple[ImageFile, ...]
    orphans: tuple[ImageFile, ...]
    duplicate_groups: tuple[tuple[ImageFile, ...], ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def orphan_bytes(self) -> int:
        return sum(item.size for item in self.orphans)

    @property
    def duplicate_bytes(self) -> int:
        """重复的图只留一份能省下的空间（每组从第二份开始算）。"""
        return sum(
            sum(item.size for item in group[1:]) for group in self.duplicate_groups
        )

    def summary(self) -> str:
        parts = [
            f"共 {len(self.files)} 张图，{size_text(self.total_bytes)}",
            f"其中没被引用的 {len(self.orphans)} 张（{size_text(self.orphan_bytes)}）",
        ]
        if self.duplicate_groups:
            parts.append(
                f"内容重复的 {len(self.duplicate_groups)} 组"
                f"（多占 {size_text(self.duplicate_bytes)}）"
            )
        return "；".join(parts)


@dataclass(frozen=True)
class DeleteResult:
    deleted: tuple[ImageFile, ...]
    skipped: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]

    @property
    def freed_bytes(self) -> int:
        return sum(item.size for item in self.deleted)

    def summary(self) -> str:
        text = f"删掉 {len(self.deleted)} 张，释放 {size_text(self.freed_bytes)}"
        if self.skipped:
            text += f"；{len(self.skipped)} 张已经被记录引用，留着"
        if self.failed:
            text += f"；{len(self.failed)} 张删不掉（文件被占用？）"
        return text


# --------------------------------------------------------------- 内部
def _referenced(repo: EntryRepository) -> set[str]:
    """库里引用到的图片路径，统一小写比较（Windows 文件名不分大小写）。"""
    try:
        return {path.casefold() for path in repo.used_images()}
    except (StorageError, OSError) as exc:
        raise MaintenanceFailed(
            "读不出数据库里引用了哪些图片，先别整理。",
            str(exc),
        ) from exc


def _iter_images(root: Path) -> list[ImageFile]:
    try:
        days = sorted(
            path
            for path in root.iterdir()
            if path.is_dir() and path.name not in SKIP_DIRS
        )
    except OSError as exc:
        raise MaintenanceFailed(
            f"读不了图片目录 {root}，检查一下 data 目录权限。",
            str(exc),
        ) from exc

    files: list[ImageFile] = []
    for day in days:
        try:
            entries = sorted(day.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append(ImageFile(path.relative_to(root).as_posix(), size))
    return files


def _duplicate_groups(
    root: Path, files: tuple[ImageFile, ...]
) -> tuple[tuple[ImageFile, ...], ...]:
    """按内容分组，只返回真重复（>=2 份）的组。

    先按大小分桶，只有大小撞上的才算哈希——体积大的照片算 sha1 也要几十毫秒，
    没必要对每张都算。
    """
    by_size: dict[int, list[ImageFile]] = {}
    for item in files:
        by_size.setdefault(item.size, []).append(item)

    groups: list[tuple[ImageFile, ...]] = []
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_digest: dict[str, list[ImageFile]] = {}
        for item in same_size:
            try:
                digest = entry_service.file_digest(root / item.relative_path)
            except OSError:
                continue
            by_digest.setdefault(digest, []).append(item)
        for same_content in by_digest.values():
            if len(same_content) > 1:
                groups.append(tuple(sorted(same_content, key=lambda i: i.relative_path)))

    groups.sort(key=lambda group: group[0].relative_path)
    return tuple(groups)


# --------------------------------------------------------------- 对外
def scan(*, root: Path | None = None, repo: EntryRepository | None = None) -> ImageReport:
    """扫一遍上传目录：总共有多少、哪些没人引用、哪些内容重复。"""
    base = root or entry_service.upload_root()
    repository = repo or EntryRepository()

    if not base.exists():
        return ImageReport(files=(), orphans=(), duplicate_groups=())

    files = tuple(_iter_images(base))
    used = _referenced(repository)
    orphans = tuple(
        item for item in files if item.relative_path.casefold() not in used
    )
    return ImageReport(
        files=files,
        orphans=orphans,
        duplicate_groups=_duplicate_groups(base, files),
    )


def delete_orphans(
    relative_paths: list[str] | tuple[str, ...],
    *,
    root: Path | None = None,
    repo: EntryRepository | None = None,
) -> DeleteResult:
    """删掉指定的图，但**只删确实没人引用的**。

    删除会顺手把空掉的日期目录也去掉，免得 data/uploads 里留一堆空文件夹。
    单张删失败（被别的程序打开着）不影响其它张，最后统一汇报。
    """
    base = root or entry_service.upload_root()
    repository = repo or EntryRepository()
    used = _referenced(repository)

    deleted: list[ImageFile] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    touched_dirs: set[Path] = set()

    for relative in relative_paths:
        if relative.casefold() in used:
            skipped.append(relative)
            continue
        path = base / relative
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            continue  # 已经不在了，当作删过
        except OSError as exc:
            failed.append((relative, str(exc)))
            continue
        deleted.append(ImageFile(relative, size))
        touched_dirs.add(path.parent)

    for folder in touched_dirs:
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass

    if deleted:
        log.info(
            "清理孤儿图片：删掉 %d 张，释放 %d 字节",
            len(deleted),
            sum(item.size for item in deleted),
        )
    return DeleteResult(
        deleted=tuple(deleted),
        skipped=tuple(skipped),
        failed=tuple(failed),
    )