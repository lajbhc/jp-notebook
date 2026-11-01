"""识别相关的统一异常。

分层约定：UI 只 catch `RecognitionError` 基类，拿 `user_message` 显示给用户；
`detail` 只用于日志和排查，不要直接甩到界面上。

这样做的原因：识别链路上会出很多种错（没配 Key、图坏了、断网、服务端 401、
返回的不是 JSON……），如果每个地方各写各的提示，界面上会出现十几种措辞不一的
报错。集中到这里，后面换模型、换接口地址都不用动 UI。
"""

from __future__ import annotations

__all__ = [
    "RecognitionError",
    "MissingApiKey",
    "ImageInvalid",
    "ApiRequestFailed",
    "ResponseNotJson",
    "NetworkTimeout",
]


class RecognitionError(Exception):
    """所有识别失败的基类。"""

    default_user_message = "识别失败，请稍后重试，或直接手动填写。"

    def __init__(self, detail: str = "", user_message: str | None = None) -> None:
        self.detail = (detail or "").strip()
        self.user_message = user_message or self.default_user_message
        super().__init__(self.detail or self.user_message)


class MissingApiKey(RecognitionError):
    """没有配置 API Key 或模型名。"""

    default_user_message = "识别接口还没配置好，请先填写 .env。"

    def __init__(self, missing: list[str] | None = None) -> None:
        self.missing = list(missing or [])
        if self.missing:
            names = "、".join(self.missing)
            message = (
                f"缺少配置项：{names}。请把它们写进项目根目录的 .env 文件，"
                "然后重新打开程序。"
            )
        else:
            message = self.default_user_message
        super().__init__(f"missing env: {self.missing}", message)


def _http_hint(status_code: int) -> str:
    """把 HTTP 状态码翻成人话。"""
    if status_code in (401, 403):
        return "API Key 无效或没有调用权限，请检查 .env 里的 OPENAI_API_KEY。"
    if status_code == 404:
        return "接口地址或模型名不对，请检查 OPENAI_BASE_URL 和 VISION_MODEL。"
    if status_code == 413:
        return "图片太大了，接口拒收。换一张小一点的图试试。"
    if status_code == 429:
        return "请求太频繁或额度用完了，等一会儿再试。"
    if status_code == 400:
        return "接口不接受这次请求，常见原因是该模型不支持图片输入，或提示词超长。"
    if 500 <= status_code < 600:
        return "识别服务端出错了，稍后重试一次。"
    return "接口返回了错误，可以重试一次。"


class ApiRequestFailed(RecognitionError):
    """接口返回了非 2xx 状态码，或请求本身发不出去。"""

    default_user_message = "识别接口调用失败，请稍后重试或手动填写。"

    def __init__(self, status_code: int | None = None, detail: str = "") -> None:
        self.status_code = status_code
        if status_code is None:
            message = "连不上识别接口，请检查网络、代理设置或 OPENAI_BASE_URL。"
        else:
            message = f"{_http_hint(status_code)}（HTTP {status_code}）"
        super().__init__(detail or f"status={status_code}", message)


class NetworkTimeout(RecognitionError):
    """连接或读取超时。"""

    default_user_message = "连接识别接口超时了，检查网络后重试一次。"


class ImageInvalid(RecognitionError):
    """图片打不开、格式不支持或过大。"""

    default_user_message = "这张图片打不开，或格式不支持，请换一张 JPG / PNG 试试。"


class ResponseNotJson(RecognitionError):
    """模型返回的内容无法解析成 JSON 对象。"""

    default_user_message = (
        "识别结果不是可解析的结构化数据，可以重试一次，或者直接手动填写。"
    )