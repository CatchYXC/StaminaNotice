"""与 XutheringWavesUID 的唯一接缝。

设计约束（很重要）：
1. ``XutheringWavesUID/__init__.py`` 在模块级就会 ``copy_build_files()``、
   ``init_localization()``、``asyncio.get_event_loop().create_task(...)``，
   所以绝不能在模块顶层 import 它，只能在 async 上下文里懒加载。
2. gsuid_core 的加载器可能把对方以不同 namespace 载入（nest 结构 / 热重载），
   因此优先从 ``sys.modules`` 里按后缀嗅探已经加载好的模块，避免让它的
   ``__init__.py`` 被二次执行（重复注册 hook / 重复建定时任务）。
3. 对方任何一个符号取不到，本插件降级为「提示用户先安装/升级
   XutheringWavesUID」，而不是抛异常拖垮整个核心。
"""

import sys
import importlib
from typing import Any, Optional, Tuple

from gsuid_core.logger import logger

XW_ROOT = "XutheringWavesUID"

_CACHE: dict = {}
_ERROR: str = ""


def _sniff(suffix: str) -> Optional[Any]:
    """在 sys.modules 里按 ``*.suffix`` 找已经加载好的模块。"""
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if name == suffix or name.endswith("." + suffix):
            return module
    return None


def _get(suffix: str) -> Any:
    """取对方模块，suffix 形如 ``utils.database.models``。"""
    if suffix in _CACHE:
        return _CACHE[suffix]

    module = _sniff(suffix)
    if module is None:
        module = importlib.import_module(f"{XW_ROOT}.{suffix}")
    _CACHE[suffix] = module
    return module


def _models() -> Any:
    return _get("utils.database.models")


def _api() -> Any:
    return _get("utils.waves_api").waves_api


def _daily_data() -> Any:
    return _get("utils.api.model").DailyData


def _launcher_chain() -> Any:
    return _get("utils.api.launcher_chain")


def ensure() -> bool:
    """预热并自检依赖，返回是否可用。"""
    global _ERROR
    try:
        _models()
        _api()
        _daily_data()
        _ERROR = ""
        return True
    except Exception as e:
        _ERROR = f"{type(e).__name__}: {e}"
        logger.warning(f"[体力提醒] 依赖 XutheringWavesUID 不可用: {_ERROR}")
        return False


def available() -> bool:
    if _CACHE:
        return not _ERROR
    return ensure()


def error() -> str:
    return _ERROR


async def get_uid(user_id: str, bot_id: str) -> Optional[str]:
    """取该平台用户的绑定 UID（XutheringWavesUID 的 WavesBind）。"""
    try:
        return await _models().WavesBind.get_uid_by_game(user_id, bot_id)
    except Exception as e:
        logger.warning(f"[体力提醒] 读取绑定 UID 失败: {e}")
        return None


async def resolve_user_id(ev: Any) -> str:
    """与 XutheringWavesUID 保持一致的「被 at 用户」解析，保证绑定能对上。"""
    try:
        return _get("utils.at_help").ruser_id(ev)
    except Exception:
        return ev.user_id


def xw_prefix() -> str:
    """取 XutheringWavesUID 当前实际生效的指令前缀，用于提示文案。"""
    try:
        from gsuid_core.sv import get_plugin_available_prefix

        return get_plugin_available_prefix(XW_ROOT) or "ww"
    except Exception:
        return "ww"


async def fetch_energy(
    uid: str,
    user_id: str,
    bot_id: str,
) -> Tuple[Optional[int], Optional[int], str]:
    """取当前体力与「体力回满」时间戳。

    Returns:
        (cur, refresh_ts, err)
        成功时 err 为空串；失败时 cur/refresh_ts 为 None，err 为可直出的中文原因。
    """
    try:
        api = _api()
    except Exception as e:
        return None, None, f"未检测到 XutheringWavesUID（{type(e).__name__}）"

    try:
        # 国际服走 launcher SDK 链路（XutheringWavesUID 已实现凭据续期）
        if api.is_net(uid):
            panel = await _launcher_chain().fetch_launcher_panel(user_id, bot_id, uid)
            if panel is None:
                return None, None, "国际服启动器面板获取失败，请重新登录"
            energy = getattr(panel, "energy", None)
            if energy is None:
                return None, None, "启动器面板缺少体力数据"
            return (
                getattr(energy, "cur", None),
                getattr(energy, "refreshTimeStamp", None),
                "",
            )

        cookie, err = await api.check_self_login(uid, user_id, bot_id)
        if not cookie:
            return None, None, err or "未绑定 cookie，请先使用「每日」完成登录"

        res = await api.get_daily_info(uid, cookie)
        if not getattr(res, "success", False):
            msg = ""
            try:
                msg = res.throw_msg() or ""
            except Exception:
                msg = ""
            return None, None, msg or "获取每日信息失败"

        daily = _daily_data().model_validate(res.data)
        energy = getattr(daily, "energyData", None)
        if energy is None:
            return None, None, "每日信息缺少体力数据"
        return (
            getattr(energy, "cur", None),
            getattr(energy, "refreshTimeStamp", None),
            "",
        )
    except Exception as e:
        logger.exception("[体力提醒] 取体力数据异常")
        return None, None, f"获取体力数据异常：{type(e).__name__}"
