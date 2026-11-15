"""与 XutheringWavesUID 的唯一接缝。

设计约束（很重要）：
1. ``XutheringWavesUID/__init__.py`` 在模块级就会 ``copy_build_files()``、
   ``init_localization()``、``asyncio.get_event_loop().create_task(...)``，
   所以绝不能在模块顶层 import 它，只能在 async 上下文里懒加载。
2. gsuid_core 的加载器可能把对方以不同 namespace 载入（nest 结构 / 热重载），
   因此优先从 ``sys.modules`` 里找已经加载好的模块，避免让它的 ``__init__.py``
   被二次执行（重复注册 hook / 重复建定时任务）。
3. 找模块时**必须**校验包名 + 校验符号。曾经只用 ``endswith("utils.database.models")``
   嗅探，结果命中了 gsuid_core 自己的 ``gsuid_core.utils.database.models``
   （它没有 WavesBind），导致「已绑定却提示未绑定」。
4. 对方任何一个符号取不到，本插件降级为「提示用户先安装/升级
   XutheringWavesUID」，而不是抛异常拖垮整个核心。
"""

import sys
import importlib
from typing import Any, Iterator, Optional, Tuple

from gsuid_core.logger import logger

XW_ROOT = "XutheringWavesUID"

# suffix -> 该模块必须导出的符号；用来把「同名但不同包」的模块排除掉
_REQUIRED: dict = {
    "utils.database.models": ("WavesBind", "WavesUser"),
    "utils.waves_api": ("waves_api",),
    "utils.api.model": ("DailyData",),
    "utils.api.launcher_chain": ("fetch_launcher_panel",),
    "utils.at_help": ("ruser_id",),
}

_CACHE: dict = {}
_ERROR: str = ""


def _iter_loaded(suffix: str) -> Iterator[Any]:
    """枚举 sys.modules 里「可能属于对方」的候选模块。

    只认两种名字，避免误伤其它插件 / 核心的同名子模块：
    - ``XutheringWavesUID.utils.xxx``
    - ``...XutheringWavesUID.utils.xxx``（nest 结构、带父命名空间装载）
    """
    exact = f"{XW_ROOT}.{suffix}"
    module = sys.modules.get(exact)
    if module is not None:
        yield module

    tail = f".{XW_ROOT}.{suffix}"
    for name, loaded in list(sys.modules.items()):
        if loaded is None or name == exact:
            continue
        if name.endswith(tail):
            yield loaded


def _resolve(suffix: str) -> Any:
    required = _REQUIRED.get(suffix, ())
    rejected = []

    for module in _iter_loaded(suffix):
        missing = [attr for attr in required if not hasattr(module, attr)]
        if not missing:
            return module
        rejected.append(f"{module.__name__}(缺 {','.join(missing)})")

    # sys.modules 里没有可用的 → 主动 import 对方顶层包
    try:
        module = importlib.import_module(f"{XW_ROOT}.{suffix}")
    except Exception as e:
        extra = f"；已跳过候选 {rejected}" if rejected else ""
        raise ImportError(f"无法导入 {XW_ROOT}.{suffix}: {e}{extra}") from e

    missing = [attr for attr in required if not hasattr(module, attr)]
    if missing:
        raise ImportError(f"{module.__name__} 缺少符号 {missing}")
    return module


def _get(suffix: str) -> Any:
    """取对方模块，suffix 形如 ``utils.database.models``。"""
    if suffix in _CACHE:
        return _CACHE[suffix]
    module = _resolve(suffix)
    _CACHE[suffix] = module
    logger.debug(f"[体力提醒] 已接入 {module.__name__}")
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
    """预热并自检依赖，返回是否可用。失败时清空缓存，避免坏结果被复用。"""
    global _ERROR
    try:
        _models()
        _api()
        _daily_data()
        _launcher_chain()
        _ERROR = ""
        return True
    except Exception as e:
        _CACHE.clear()
        _ERROR = f"{type(e).__name__}: {e}"
        logger.warning(f"[体力提醒] 依赖 XutheringWavesUID 不可用: {_ERROR}")
        return False


def available() -> bool:
    if _CACHE and not _ERROR:
        return True
    return ensure()


def describe() -> str:
    """已解析到的模块清单，写进启动日志便于排障。"""
    if not _CACHE:
        return "（未解析）"
    return ", ".join(sorted(m.__name__ for m in _CACHE.values()))


def error() -> str:
    return _ERROR


async def get_uid(user_id: str, bot_id: str) -> Optional[str]:
    """取该平台用户的绑定 UID（XutheringWavesUID 的 WavesBind）。"""
    try:
        return await _models().WavesBind.get_uid_by_game(user_id, bot_id)
    except Exception as e:
        logger.warning(f"[体力提醒] 读取绑定 UID 失败: {type(e).__name__}: {e}")
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
