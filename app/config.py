"""项目路径与环境变量配置。

设计约束
--------
1. 本模块只依赖标准库与 python-dotenv，不向上依赖 services / storage / ui，
   保证分层方向单一：ui -> services -> storage，且任何层都能安全 import config。
2. 缺少 API Key 时返回 ``None`` 而不是抛异常。原因：配置不完整不应该导致程序
   无法启动（用户仍然可以查看词库），"是否可用"的判断留给 services 层，
   由它抛出带 user_message 的 RecognitionError 给 UI 展示。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------- 路径
BASE_DIR: Path = Path(__file__).resolve().parent.parent
ENV_FILE: Path = BASE_DIR / ".env"

DATA_DIR: Path = BASE_DIR / "data"
UPLOAD_DIR: Path = DATA_DIR / "uploads"
LOG_DIR: Path = DATA_DIR / "logs"
DB_PATH: Path = DATA_DIR / "app.db"

# ---------------------------------------------------------------- 默认值
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_UPLOAD_PORT = 8765
DEFAULT_REMINDER_TIME = "21:00"

# 识别功能必需的变量，缺任意一个都不能识别。
# OPENAI_BASE_URL 不在此列：它有官方默认值（见 DEFAULT_BASE_URL），永远不为空，
# 放进来只会是一条永远命中不了的死判断。用第三方兼容端点时在 .env 里显式覆盖。
REQUIRED_ENV_NAMES = ("OPENAI_API_KEY", "VISION_MODEL")


def ensure_dirs() -> None:
    """创建运行期需要的目录（幂等）。"""
    for directory in (DATA_DIR, UPLOAD_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _env(name: str) -> str | None:
    """读取环境变量，去空白；空字符串视为未设置。"""
    value = os.getenv(name, "")
    value = value.strip()
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        # 端口写错不该让程序起不来，回退默认值即可
        return default


@dataclass(frozen=True)
class Settings:
    """一次读取、全程使用的运行配置快照。"""

    api_key: str | None
    base_url: str
    vision_model: str | None
    upload_port: int
    reminder_time: str
    # 补全用的模型（纯文本）。不给就用 VISION_MODEL——同一个端点通常也能跑文本，
    # 想换成更便宜/更擅长写作的模型时在 .env 里单独写 TEXT_MODEL 即可。
    text_model: str | None = None

    @property
    def missing_required(self) -> list[str]:
        """返回仍然缺失的必需环境变量名，用于界面提示。"""
        values = {
            "OPENAI_API_KEY": self.api_key,
            "OPENAI_BASE_URL": self.base_url,
            "VISION_MODEL": self.vision_model,
        }
        return [name for name in REQUIRED_ENV_NAMES if not values[name]]

    @property
    def recognition_ready(self) -> bool:
        """识别链路所需的配置是否齐全。"""
        return not self.missing_required

    @property
    def enrichment_model(self) -> str | None:
        """补全链路要用的模型：优先 TEXT_MODEL，没配就复用视觉模型。"""
        return self.text_model or self.vision_model


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """加载 .env 并返回配置快照（进程内缓存）。"""
    load_dotenv(ENV_FILE, override=False)
    base_url = _env("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    return Settings(
        api_key=_env("OPENAI_API_KEY"),
        base_url=base_url.rstrip("/"),
        vision_model=_env("VISION_MODEL"),
        upload_port=_env_int("UPLOAD_PORT", DEFAULT_UPLOAD_PORT),
        reminder_time=_env("REMINDER_TIME") or DEFAULT_REMINDER_TIME,
        text_model=_env("TEXT_MODEL"),
    )


def reload_settings() -> Settings:
    """清空缓存后重新读取，供修改 .env 后热加载或测试使用。"""
    get_settings.cache_clear()
    return get_settings()