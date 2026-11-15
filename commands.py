"""用户指令（全部按「当前会话」定位目标）。

指令词（前缀统一为 ww，与 XutheringWavesUID 一致）：
    ww开启体力提醒 / ww关闭体力提醒      （私聊里改私聊那行，群里改本群那行）
    ww关闭全部提醒                       （私聊 + 所有群一次性清掉）
    ww开启催命模式 / ww关闭催命模式      （同样只作用于当前会话）
    ww设置提醒阈值240 / ww设置提醒阈值   （不带数字=查状态）
    ww体力提醒状态

在设计上，数据库是「一个推送目标一行」，所以这里的每条指令都只改
当前会话对应的那一条记录：群里发就改群里的阈值，私聊发就改私聊的阈值。
"""

import time
from typing import List, Tuple

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

from . import stamina_config as cfg
from . import xw_bridge
from .models import (
    TARGET_GROUP,
    TARGET_PRIVATE,
    WavesStaminaNotice,
)
from .push_engine import compute_next_check

sv_notice = SV("ww体力提醒")
sv_crazy = SV("ww催命模式")
# priority=1：XutheringWavesUID 的「waves配置」用 on_prefix("设置", block=True)
# 且 priority=3，会把「设置XXX」整段吞掉。这里必须抢在它前面并 block。
sv_threshold = SV("ww设置提醒阈值", priority=1)

XW_MISSING = (
    "❌ 未检测到可用的 XutheringWavesUID\n"
    "请先安装/启用 XutheringWavesUID 后重试"
)


async def _prepare(ev: Event):
    """解析当前用户与其绑定 UID。

    Returns:
        (user_id, uid, error_msg)
    """
    user_id = await xw_bridge.resolve_user_id(ev)
    if not xw_bridge.available():
        return user_id, None, XW_MISSING
    uid = await xw_bridge.get_uid(user_id, ev.bot_id)
    if not uid:
        prefix = xw_bridge.xw_prefix()
        return user_id, None, f"❌ 你还没有绑定鸣潮 UID，请先发送 [{prefix}绑定uid]"
    return user_id, uid, ""


def _ctx(ev: Event) -> Tuple[str, str, str]:
    """当前会话对应的推送目标： (target_type, target_id, 文案)。"""
    if ev.group_id:
        return TARGET_GROUP, str(ev.group_id), "本群"
    return TARGET_PRIVATE, "", "私聊"


def _group_admin_error(ev: Event) -> str:
    if ev.group_id and ev.user_pm > 3:
        return "❌ 群内开启/修改体力提醒需要群管理权限\n💡 也可以私聊我单独设置私聊提醒"
    return ""


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "即将检查"
    if ts <= time.time():
        return "即将检查"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _row_text(row: WavesStaminaNotice, mark: str = "") -> str:
    crazy = "，🔥催命开启" if row.crazy == "on" else ""
    return (
        f"{mark}{row.target_label}（记录#{row.id}）：阈值 {row.threshold}"
        f"{crazy}，下次检查 {_fmt_ts(row.next_check_ts)}"
    )


def _rows_text(rows: List[WavesStaminaNotice], ctx: Tuple[str, str, str]) -> str:
    ctx_type, ctx_id, _ = ctx
    lines = []
    for row in rows:
        mark = "● " if (row.target_type == ctx_type and row.target_id == ctx_id) else "· "
        lines.append(_row_text(row, mark))
    return "\n".join(lines)


@sv_notice.on_fullmatch(
    ("开启体力提醒", "打开体力提醒", "订阅体力提醒", "开启体力通知", "开启本群体力提醒"),
    block=True,
)
async def open_notice(bot: Bot, ev: Event):
    admin_err = _group_admin_error(ev)
    if admin_err:
        return await bot.send(admin_err)

    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    target_type, target_id, label = _ctx(ev)

    # 开启时立刻采样一次：既校验登录态，又记录基线体力
    cur, refresh_ts, fetch_err = await xw_bridge.fetch_energy(uid, user_id, ev.bot_id)
    if cur is None:
        return await bot.send(f"❌ 开启失败：{fetch_err}")

    old = await WavesStaminaNotice.get_row(
        user_id, ev.bot_id, uid, target_type, target_id
    )
    threshold = old.threshold if (old and old.threshold) else cfg.threshold_max()
    next_check = compute_next_check(int(time.time()), cur, threshold, refresh_ts or 0)

    await WavesStaminaNotice.upsert_row(
        user_id,
        ev.bot_id,
        uid,
        target_type,
        target_id,
        bot_self_id=ev.bot_self_id or "",
        threshold=threshold,
        last_energy=cur,
        next_check_ts=next_check,
        fail_count=0,
        last_error="",
    )

    prefix = xw_bridge.xw_prefix()
    lines = [
        f"✅ 已开启{label}体力提醒\n",
        f"🕒 当前体力：{cur}，阈值：{threshold}\n",
    ]
    if cur >= threshold:
        lines.append("⚠️ 当前体力已达阈值，本次不重复推送；清完体力后会自动开始计时\n")
    logger.info(
        f"[鸣潮体力提醒] 开启 uid={uid} user={user_id} target={target_type}:{target_id or '-'}"
    )
    return await bot.send("".join(lines))


@sv_notice.on_fullmatch(
    ("关闭体力提醒", "关掉体力提醒", "取消体力提醒", "关闭体力通知", "关闭本群体力提醒"),
    block=True,
)
async def close_notice(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    target_type, target_id, label = _ctx(ev)
    deleted = await WavesStaminaNotice.delete_row(
        user_id, ev.bot_id, uid, target_type, target_id
    )
    if not deleted:
        return await bot.send(f"❌ 你当前没有开启{label}体力提醒")

    rest = await WavesStaminaNotice.list_rows(user_id, ev.bot_id, uid)
    logger.info(
        f"[鸣潮体力提醒] 关闭 uid={uid} target={target_type}:{target_id or '-'} 剩余 {len(rest)} 条"
    )
    lines = [f"❌ 已关闭{label}的体力提醒"]
    return await bot.send("".join(lines))


@sv_notice.on_fullmatch(("关闭全部提醒", "关闭全部体力提醒"), block=True)
async def close_all(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    count = await WavesStaminaNotice.delete_all(user_id, ev.bot_id, uid)
    if not count:
        return await bot.send("❌ 你当前没有任何体力提醒")
    logger.info(f"[鸣潮体力提醒] 关闭全部 uid={uid} user={user_id} count={count}")
    return await bot.send(f"❌ 已关闭全部体力提醒（共 {count} 个目标，含催命模式）")


@sv_crazy.on_fullmatch(("开启催命模式", "打开催命模式"), block=True)
async def open_crazy(bot: Bot, ev: Event):
    admin_err = _group_admin_error(ev)
    if admin_err:
        return await bot.send(admin_err)

    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)
    if not cfg.crazy_enabled():
        return await bot.send("❌ 管理员已关闭催命模式总开关")

    target_type, target_id, label = _ctx(ev)
    record = await WavesStaminaNotice.get_row(
        user_id, ev.bot_id, uid, target_type, target_id
    )
    if record is None:
        return await bot.send(
            f"❌ 你还没有开启{label}体力提醒\n"
        )

    await WavesStaminaNotice.set_fields(
        record.id,
        crazy="on",
        next_check_ts=int(time.time()),
        last_push_ts=0,
    )
    logger.info(f"[鸣潮体力提醒] 开启催命 uid={uid} target={target_type}:{target_id or '-'}")
    return await bot.send(
        f"✅ 已开启{label}催命模式\n"
        f"⏰体力达到阈值 {record.threshold} 后，每 {cfg.crazy_minutes()} 分钟提醒一次\n"
    )


@sv_crazy.on_fullmatch(("关闭催命模式", "关掉催命模式"), block=True)
async def close_crazy(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    target_type, target_id, label = _ctx(ev)
    record = await WavesStaminaNotice.get_row(
        user_id, ev.bot_id, uid, target_type, target_id
    )
    if record is None or record.crazy != "on":
        return await bot.send(f"❌ 你当前没有开启{label}催命模式")

    await WavesStaminaNotice.set_fields(
        record.id,
        crazy="off",
        next_check_ts=int(time.time()) + 60,
    )
    logger.info(f"[鸣潮体力提醒] 关闭催命 uid={uid} target={target_type}:{target_id or '-'}")
    return await bot.send(f"❌ 已关闭{label}催命模式，体力提醒仍然保留")


@sv_threshold.on_regex(r"^(?:设置)?提醒阈值\s*(?P<num>\d{1,3})$", block=True)
async def set_threshold(bot: Bot, ev: Event):
    admin_err = _group_admin_error(ev)
    if admin_err:
        return await bot.send(admin_err)

    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    raw = ev.regex_dict.get("num") or ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return await bot.send("❌ 阈值必须是数字")

    cap = cfg.threshold_max()
    if value < 1 or value > cap:
        return await bot.send(f"❌ 阈值需要在 1 ~ {cap} 之间")

    target_type, target_id, label = _ctx(ev)
    record = await WavesStaminaNotice.get_row(
        user_id, ev.bot_id, uid, target_type, target_id
    )
    if record is None:
        return await bot.send(
            f"❌ 你还没有开启{label}体力提醒\n"
            f"🔧 先发送 [{xw_bridge.xw_prefix()}开启体力提醒]，再设置阈值"
        )

    cur, refresh_ts, fetch_err = await xw_bridge.fetch_energy(uid, user_id, ev.bot_id)
    if cur is None:
        return await bot.send(f"❌ 设置失败：{fetch_err}")

    await WavesStaminaNotice.set_fields(
        record.id,
        threshold=value,
        last_energy=cur,
        next_check_ts=compute_next_check(
            int(time.time()), cur, value, refresh_ts or 0
        ),
        last_error="",
    )
    logger.info(
        f"[鸣潮体力提醒] 设置阈值 uid={uid} target={target_type}:{target_id or '-'} -> {value}"
    )
    return await bot.send(
        f"✅ {label}提醒阈值已设置为 {value}（当前体力 {cur}）"
    )


@sv_threshold.on_fullmatch(
    ("设置提醒阈值", "提醒阈值", "查看提醒阈值", "查看体力提醒", "体力提醒状态"),
    block=True,
)
async def show_status(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    ctx = _ctx(ev)
    rows = await WavesStaminaNotice.list_rows(user_id, ev.bot_id, uid)
    if not rows:
        return await bot.send(
            "❌ 当前未开启体力提醒\n"
        )

    lines = [
        "📋 体力提醒状态（● 为当前会话）\n",
        _rows_text(rows, ctx) + "\n",
    ]
    total = sum(r.push_count or 0 for r in rows)
    lines.append(f"\n📊 累计推送：{total} 次")
    errors = [r for r in rows if r.last_error]
    if errors:
        lines.append(f"\n⚠️ {errors[0].target_label} 上次取数错误：{errors[0].last_error}")
    lines.append(
        f"\n🔧 改本会话阈值：[{xw_bridge.xw_prefix()}设置提醒阈值240]"
    )
    return await bot.send("".join(lines))
