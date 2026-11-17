"""配置读取封装。

对外只暴露语义化函数，并给每个键兜底缺省值：
即使 config.json 里缺键、或 gsuid_core 版本差异导致 get_config 抛错，
调用方也不会因为配置问题崩掉。
"""

from typing import Any

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .config_default import CONFIG_DEFAULT

CONFIG_PATH = get_res_path("StaminaNotice") / "config.json"

StaminaNoticeConfig = StringConfig(
    "鸣潮体力提醒",
    CONFIG_PATH,
    CONFIG_DEFAULT,
)


def get_conf(key: str, default: Any = None) -> Any:
    """安全读取单个配置项。"""
    try:
        value = StaminaNoticeConfig.get_config(key).data
    except Exception:
        return default
    return default if value is None else value


def _as_int(value: Any, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def push_enabled() -> bool:
    return bool(get_conf("StaminaNotice", True))


def crazy_enabled() -> bool:
    return bool(get_conf("CrazyNotice", True))


def check_minutes() -> int:
    return _as_int(get_conf("CheckMinutes"), 6, 1)


def crazy_minutes() -> int:
    return _as_int(get_conf("CrazyMinutes"), 60, 6)


def recheck_minutes() -> int:
    return _as_int(get_conf("RecheckMinutes"), 120, 6)


def threshold_max() -> int:
    return _as_int(get_conf("ThresholdMax"), 240, 1)


def push_image() -> bool:
    """提醒是否渲染成图片推送（关掉就一直发纯文字）。"""
    return bool(get_conf("PushImage", True))


def debug_enabled() -> bool:
    """调试模式：解锁「测试推送」指令，并打印判定明细日志。"""
    return bool(get_conf("DebugMode", False))
