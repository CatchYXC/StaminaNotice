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
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Tuple

from gsuid_core.logger import logger

XW_ROOT = "XutheringWavesUID"

# suffix -> 该模块必须导出的符号；用来把「同名但不同包」的模块排除掉
_REQUIRED: dict = {
    "utils.database.models": ("WavesBind", "WavesUser"),
    "utils.waves_api": ("waves_api",),
    "utils.api.model": ("DailyData", "AccountBaseInfo"),
    "utils.api.launcher_chain": ("fetch_launcher_panel",),
    "utils.at_help": ("ruser_id",),
    # 推送图片直接复用「每日」的绘图入口，不再自己拼图
    "wutheringwaves_stamina.draw_waves_stamina": ("draw_stamina_img",),
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
    logger.debug(f"[鸣潮体力提醒] 已接入 {module.__name__}")
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
        logger.warning(f"[鸣潮体力提醒] 依赖 XutheringWavesUID 不可用: {_ERROR}")
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
        logger.warning(f"[鸣潮体力提醒] 读取绑定 UID 失败: {type(e).__name__}: {e}")
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


@dataclass
class StaminaSnapshot:
    """一次体力采样。判定只用 ``cur`` / ``refresh_ts``。"""

    uid: str
    cur: int
    total: int = 240
    refresh_ts: int = 0


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


async def fetch_snapshot(
    uid: str,
    user_id: str,
    bot_id: str,
) -> Tuple[Optional[StaminaSnapshot], str]:
    """取一次体力快照（``cur`` + 体力回满时间戳）。

    只打一次接口：国服走 ``get_daily_info``，国际服走启动器面板。
    """
    try:
        api = _api()
    except Exception as e:
        return None, f"未检测到 XutheringWavesUID（{type(e).__name__}）"

    try:
        # 国际服走 launcher SDK 链路（XutheringWavesUID 已实现凭据续期）
        if api.is_net(uid):
            panel = await _launcher_chain().fetch_launcher_panel(user_id, bot_id, uid)
            if panel is None:
                return None, "国际服启动器面板获取失败，请重新登录"
            energy = getattr(panel, "energy", None)
            if energy is None:
                return None, "启动器面板缺少体力数据"
            return StaminaSnapshot(
                uid=str(uid),
                cur=_safe_int(getattr(energy, "cur", None)) or 0,
                total=_safe_int(getattr(energy, "total", None)) or 240,
                refresh_ts=_safe_int(getattr(energy, "refreshTimeStamp", None)) or 0,
            ), ""

        cookie, err = await api.check_self_login(uid, user_id, bot_id)
        if not cookie:
            return None, err or "未绑定 cookie，请先使用「每日」完成登录"

        res = await api.get_daily_info(uid, cookie)
        if not getattr(res, "success", False):
            msg = ""
            try:
                msg = res.throw_msg() or ""
            except Exception:
                msg = ""
            return None, msg or "获取每日信息失败"

        daily = _daily_data().model_validate(res.data)
        energy = getattr(daily, "energyData", None)
        if energy is None:
            return None, "每日信息缺少体力数据"
        return StaminaSnapshot(
            uid=str(getattr(daily, "roleId", None) or uid),
            cur=_safe_int(getattr(energy, "cur", None)) or 0,
            total=_safe_int(getattr(energy, "total", None)) or 240,
            refresh_ts=_safe_int(getattr(energy, "refreshTimeStamp", None)) or 0,
        ), ""
    except Exception as e:
        logger.exception("[鸣潮体力提醒] 取体力数据异常")
        return None, f"获取体力数据异常：{type(e).__name__}"


class PushEvent:
    """伪造一个「够用」的 Event，喂给对方的绘图入口。

    对方 ``draw_stamina_img(bot, ev)`` 只用到这几个字段：
    ``user_id`` / ``bot_id`` / ``bot_self_id`` / ``at`` / ``sender``。
    ``at`` 留空 -> ``ruser_id(ev)`` 回落成 ``user_id``；
    ``sender`` 留空字典 -> ``get_event_avatar`` 走「按 user_id 取 QQ 头像」或
    角色头像兜底，不会因为拿不到消息体而炸。
    """

    def __init__(self, user_id: str, bot_id: str, bot_self_id: str = "") -> None:
        self.user_id = user_id
        self.bot_id = bot_id
        self.bot_self_id = bot_self_id or ""
        self.at = ""
        self.sender: dict = {}
        self.group_id = None
        self.text = ""
        self.raw_text = ""
        self.msg = ""


async def fetch_daily_image(
    uid: str,
    user_id: str,
    bot_id: str,
    bot_self_id: str = "",
) -> Tuple[Optional[bytes], str]:
    """直接触发 XutheringWavesUID「每日」的绘图入口，把图片字节拿回来。

    与其自己照着素材拼一张，不如复用对方的 ``draw_stamina_img``：
    版式、素材、头像、背景/立绘配置全部和用户 `ww每日` 看到的一模一样。

    注意：对方绘图时会顺手把这次查询写进它自己的 ``WavesStaminaRecord``
    （``upsert_stamina_query``，记录查询时间与体力值），属于「真的查了一次」的
    正常记账，不影响本插件的计时。

    Returns:
        (jpeg_bytes, err)。失败时 bytes 为 None，err 为可直出的中文原因。
    """
    try:
        mod = _get("wutheringwaves_stamina.draw_waves_stamina")
    except Exception as e:
        logger.warning(f"[鸣潮体力提醒] 未找到「每日」绘图模块：{e}")
        return None, f"未检测到 XutheringWavesUID 的每日绘图模块（{type(e).__name__}）"

    ev = PushEvent(user_id, bot_id, bot_self_id)
    try:
        # bot 参数对方内部没用到，传 None 即可
        res = await mod.draw_stamina_img(None, ev)
    except Exception as e:
        logger.exception(f"[鸣潮体力提醒] 调用每日绘图失败 uid={uid}")
        return None, f"每日图片渲染异常：{type(e).__name__}"

    if isinstance(res, (bytes, bytearray)):
        return bytes(res), ""
    if isinstance(res, str):
        # 对方把「未绑定 / CK 失效 / 维护中」等直接以字符串返回
        return None, res.strip() or "每日图片生成失败"

    # 兜底：万一对方改了返回类型（PIL Image）
    try:
        import io

        buf = io.BytesIO()
        res.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue(), ""
    except Exception:
        return None, "每日图片格式无法识别"


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
    snap, err = await fetch_snapshot(uid, user_id, bot_id)
    if snap is None:
        return None, None, err
    return snap.cur, snap.refresh_ts, ""

