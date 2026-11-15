"""用户指令。

指令词（前缀统一为 ww，与 XutheringWavesUID 一致）：
    ww开启体力提醒 / ww关闭体力提醒
    ww开启催命模式 / ww关闭催命模式
    ww体力提醒阈值240 / ww体力提醒阈值
    ww体力提醒状态
"""

import time

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

from . import stamina_config as cfg
from . import xw_bridge
from .models import (
    TARGET_GROUP,
    TARGET_OFF,
    TARGET_PRIVATE,
    WavesStaminaNotice,
)
from .push_engine import compute_next_check

sv_notice = SV("ww体力提醒")
sv_crazy = SV("ww催命模式")
sv_threshold = SV("ww体力提醒阈值")

XW_MISSING = (
    "❌ 未检测到可用的 XutheringWavesUID\n"
    "本插件只借用它的账号与体力接口，请先安装/启用 XutheringWavesUID 后重试"
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


def _group_hint(ev: Event) -> str:
    return ev.group_id or ""


@sv_notice.on_fullmatch(
    ("开启体力提醒", "打开体力提醒", "订阅体力提醒", "开启体力通知"),
    block=True,
)
async def open_notice(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    # 群内开启 = 推送到该群，需要群管理，避免普通成员刷群
    group_id = _group_hint(ev)
    if group_id:
        if ev.user_pm > 3:
            return await bot.send("❌ 群内开启体力提醒需要群管理权限，或私聊我单独开启")
        target = TARGET_GROUP
    else:
        target = TARGET_PRIVATE

    # 开启时立刻采样一次：既校验登录态，又记录基线体力
    cur, refresh_ts, fetch_err = await xw_bridge.fetch_energy(uid, user_id, ev.bot_id)
    if cur is None:
        return await bot.send(f"❌ 开启失败：{fetch_err}")

    threshold = cfg.threshold_max()
    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is not None and record.threshold:
        threshold = record.threshold

    next_check = compute_next_check(int(time.time()), cur, threshold, refresh_ts or 0)
    await WavesStaminaNotice.upsert(
        user_id,
        ev.bot_id,
        uid,
        bot_self_id=ev.bot_self_id or "",
        push_target=target,
        group_id=group_id,
        threshold=threshold,
        last_energy=cur,
        next_check_ts=next_check,
        last_error="",
    )

    where = f"群 {group_id}" if target == TARGET_GROUP else "私聊"
    lines = [
        "✅ 已开启体力提醒\n",
        f"🎯 推送位置：{where}\n",
        f"🕒 当前体力：{cur}，阈值：{threshold}\n",
    ]
    if cur >= threshold:
        lines.append("⚠️ 当前体力已达阈值，本次不重复推送；清完体力后会自动开始计时\n")
    lines.append(
        f"🔧 修改阈值：[{xw_bridge.xw_prefix()}体力提醒阈值240]\n"
        f"🔧 关闭提醒：[{xw_bridge.xw_prefix()}关闭体力提醒]"
    )
    logger.info(f"[体力提醒] 开启 uid={uid} user={user_id} target={target}")
    return await bot.send("".join(lines))


@sv_notice.on_fullmatch(
    ("关闭体力提醒", "关掉体力提醒", "取消体力提醒", "关闭体力通知"),
    block=True,
)
async def close_notice(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None or record.push_target == TARGET_OFF:
        return await bot.send("❌ 你当前没有开启体力提醒")

    await WavesStaminaNotice.set_fields(
        user_id,
        ev.bot_id,
        uid,
        push_target=TARGET_OFF,
        group_id="",
        crazy="off",
        next_check_ts=0,
    )
    logger.info(f"[体力提醒] 关闭 uid={uid} user={user_id}")
    return await bot.send("❌ 已关闭体力提醒（含催命模式）")


@sv_crazy.on_fullmatch(("开启催命模式", "打开催命模式"), block=True)
async def open_crazy(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)
    if not cfg.crazy_enabled():
        return await bot.send("❌ 管理员已关闭催命模式总开关")

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None or record.push_target == TARGET_OFF:
        return await bot.send(
            f"❌ 请先发送 [{xw_bridge.xw_prefix()}开启体力提醒] 再开启催命模式"
        )

    await WavesStaminaNotice.set_fields(
        user_id,
        ev.bot_id,
        uid,
        crazy="on",
        next_check_ts=int(time.time()),
        last_push_ts=0,
    )
    logger.info(f"[体力提醒] 开启催命 uid={uid}")
    return await bot.send(
        "✅ 已开启催命模式\n"
        f"⏰体力达到阈值 {record.threshold} 后，每 {cfg.crazy_minutes()} 分钟提醒一次\n"
        f"🔧 发送 [{xw_bridge.xw_prefix()}关闭催命模式] 可关闭"
    )


@sv_crazy.on_fullmatch(("关闭催命模式", "关掉催命模式"), block=True)
async def close_crazy(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None or record.crazy != "on":
        return await bot.send("❌ 你当前没有开启催命模式")

    await WavesStaminaNotice.set_fields(
        user_id,
        ev.bot_id,
        uid,
        crazy="off",
        next_check_ts=int(time.time()) + 60,
    )
    logger.info(f"[体力提醒] 关闭催命 uid={uid}")
    return await bot.send("❌ 已关闭催命模式，体力提醒仍然保留")


@sv_threshold.on_regex(r"^体力提醒阈值\s*(?P<num>\d{1,3})$", block=True)
async def set_threshold(bot: Bot, ev: Event):
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

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None or record.push_target == TARGET_OFF:
        return await bot.send(
            f"❌ 请先发送 [{xw_bridge.xw_prefix()}开启体力提醒] 再设置阈值"
        )

    cur, refresh_ts, fetch_err = await xw_bridge.fetch_energy(uid, user_id, ev.bot_id)
    if cur is None:
        return await bot.send(f"❌ 设置失败：{fetch_err}")

    await WavesStaminaNotice.upsert(
        user_id,
        ev.bot_id,
        uid,
        bot_self_id=ev.bot_self_id or "",
        threshold=value,
        last_energy=cur,
        next_check_ts=compute_next_check(
            int(time.time()), cur, value, refresh_ts or 0
        ),
        last_error="",
    )
    logger.info(f"[体力提醒] 设置阈值 uid={uid} threshold={value}")
    return await bot.send(f"✅ 体力提醒阈值已设置为 {value}（当前体力 {cur}）")


@sv_threshold.on_fullmatch(("体力提醒阈值", "查看体力提醒", "体力提醒状态"), block=True)
async def show_status(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None or record.push_target == TARGET_OFF:
        return await bot.send(
            f"❌ 当前未开启体力提醒\n"
            f"🔧 发送 [{xw_bridge.xw_prefix()}开启体力提醒] 即可开启"
        )

    where = f"群 {record.group_id}" if record.push_target == TARGET_GROUP else "私聊"
    next_ts = record.next_check_ts or 0
    next_text = (
        time.strftime("%m-%d %H:%M", time.localtime(next_ts))
        if next_ts > time.time()
        else "即将检查"
    )
    lines = [
        "📋 体力提醒状态\n",
        f"🎯 推送位置：{where}\n",
        f"🕒 阈值：{record.threshold}\n",
        f"🔥 催命模式：{'开启' if record.crazy == 'on' else '关闭'}\n",
        f"⏱ 下次检查：{next_text}\n",
        f"📊 累计推送：{record.push_count} 次",
    ]
    if record.last_error:
        lines.append(f"\n⚠️ 上次取数错误：{record.last_error}")
    return await bot.send("".join(lines))
