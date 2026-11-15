"""用户指令。

指令词（前缀统一为 ww，与 XutheringWavesUID 一致）：
    ww开启体力提醒 / ww关闭体力提醒
    ww开启群提醒 / ww关闭群提醒          （只针对当前群，需群管理）
    ww开启催命模式 / ww关闭催命模式
    ww设置提醒阈值240 / ww设置提醒阈值   （不带数字=查状态）
    ww体力提醒状态

推送目标不是互斥的：私聊和群可以同时开。群里发「开启体力提醒」= 私聊开 + 本群开。
"""

import time

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

from . import stamina_config as cfg
from . import xw_bridge
from .models import TARGET_OFF, WavesStaminaNotice
from .push_engine import compute_next_check

sv_notice = SV("ww体力提醒")
sv_crazy = SV("ww催命模式")
# priority=1：XutheringWavesUID 的「waves配置」用 on_prefix("设置", block=True)
# 且 priority=3，会把「设置XXX」整段吞掉。这里必须抢在它前面并 block。
sv_threshold = SV("ww设置提醒阈值", priority=1)

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


def _group_admin_error(ev: Event) -> str:
    if not ev.group_id:
        return "❌ 该指令需要在群里使用"
    if ev.user_pm > 3:
        return "❌ 群提醒需要群管理才可设置"
    return ""


def _targets_text(record: WavesStaminaNotice) -> str:
    parts = []
    parts.append("📩 私聊：" + ("开启" if record.private_on else "关闭"))
    if record.groups:
        parts.append("👥 群：" + "、".join(record.groups))
    else:
        parts.append("👥 群：未开启")
    return "\n".join(parts)


@sv_notice.on_fullmatch(
    ("开启体力提醒", "打开体力提醒", "订阅体力提醒", "开启体力通知"),
    block=True,
)
async def open_notice(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    group_id = ev.group_id or ""
    if group_id and ev.user_pm > 3:
        return await bot.send(
            "❌ 群内开启体力提醒需要群管理权限\n"
            f"🔧 你可以私聊我发送 [{xw_bridge.xw_prefix()}开启体力提醒] 只开私聊推送"
        )

    # 开启时立刻采样一次：既校验登录态，又记录基线体力
    cur, refresh_ts, fetch_err = await xw_bridge.fetch_energy(uid, user_id, ev.bot_id)
    if cur is None:
        return await bot.send(f"❌ 开启失败：{fetch_err}")

    old = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    threshold = old.threshold if (old and old.threshold) else cfg.threshold_max()
    next_check = compute_next_check(int(time.time()), cur, threshold, refresh_ts or 0)

    await WavesStaminaNotice.upsert(
        user_id,
        ev.bot_id,
        uid,
        bot_self_id=ev.bot_self_id or "",
        push_private=1,
        threshold=threshold,
        last_energy=cur,
        next_check_ts=next_check,
        fail_count=0,
        last_error="",
        # 清掉 v1 遗留单目标，避免旧逻辑复活
        push_target=TARGET_OFF,
        group_id="",
    )

    if group_id:
        await WavesStaminaNotice.add_group(
            user_id, ev.bot_id, uid, group_id, ev.bot_self_id or ""
        )

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    lines = ["✅ 已开启体力提醒\n"]
    if record is not None:
        lines.append(_targets_text(record) + "\n")
    lines.append(f"🕒 当前体力：{cur}，阈值：{threshold}\n")
    if cur >= threshold:
        lines.append("⚠️ 当前体力已达阈值，本次不重复推送；清完体力后会自动开始计时\n")
    prefix = xw_bridge.xw_prefix()
    lines.append(
        f"🔧 阈值：[{prefix}设置提醒阈值240]\n"
        f"🔧 关闭：[{prefix}关闭体力提醒]"
    )
    if group_id:
        lines.append(f"\n🔧 只关这个群：[{prefix}关闭群提醒]")
    logger.info(f"[体力提醒] 开启 uid={uid} user={user_id} group={group_id or '-'}")
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
    if record is None or not record.is_enabled:
        return await bot.send("❌ 你当前没有开启体力提醒")

    await WavesStaminaNotice.set_fields(
        user_id,
        ev.bot_id,
        uid,
        push_private=0,
        group_ids="",
        push_target=TARGET_OFF,
        group_id="",
        crazy="off",
        next_check_ts=0,
    )
    logger.info(f"[体力提醒] 关闭全部 uid={uid} user={user_id}")
    return await bot.send("❌ 已关闭体力提醒（私聊 + 所有群，含催命模式）")


@sv_notice.on_fullmatch(("开启群提醒", "开启本群体力提醒"), block=True)
async def open_group_notice(bot: Bot, ev: Event):
    err = _group_admin_error(ev)
    if err:
        return await bot.send(err)

    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    group_id = ev.group_id or ""
    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None:
        # 还没有任何订阅：建行，但只开群，不动私聊开关
        cur, refresh_ts, fetch_err = await xw_bridge.fetch_energy(
            uid, user_id, ev.bot_id
        )
        if cur is None:
            return await bot.send(f"❌ 开启失败：{fetch_err}")
        threshold = cfg.threshold_max()
        await WavesStaminaNotice.upsert(
            user_id,
            ev.bot_id,
            uid,
            bot_self_id=ev.bot_self_id or "",
            push_private=0,
            threshold=threshold,
            last_energy=cur,
            next_check_ts=compute_next_check(
                int(time.time()), cur, threshold, refresh_ts or 0
            ),
            push_target=TARGET_OFF,
            group_id="",
        )
    else:
        await WavesStaminaNotice.add_group(
            user_id, ev.bot_id, uid, group_id, ev.bot_self_id or ""
        )

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    logger.info(f"[体力提醒] 开启群提醒 uid={uid} group={group_id}")
    lines = [f"✅ 已在本群开启体力提醒（群 {group_id}）\n"]
    if record is not None:
        lines.append(_targets_text(record) + "\n")
    lines.append(f"🔧 关闭本群：[{xw_bridge.xw_prefix()}关闭群提醒]")
    return await bot.send("".join(lines))


@sv_notice.on_fullmatch(("关闭群提醒", "关闭本群体力提醒"), block=True)
async def close_group_notice(bot: Bot, ev: Event):
    err = _group_admin_error(ev)
    if err:
        return await bot.send(err)

    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    group_id = ev.group_id or ""
    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None or group_id not in record.groups:
        return await bot.send("❌ 你当前没有在本群开启体力提醒")

    await WavesStaminaNotice.remove_group(user_id, ev.bot_id, uid, group_id)
    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    logger.info(f"[体力提醒] 关闭群提醒 uid={uid} group={group_id}")

    lines = [f"❌ 已关闭本群（{group_id}）的体力提醒\n"]
    if record is not None:
        lines.append(_targets_text(record))
        if not record.is_enabled:
            lines.append("\n💡 私聊和群都已关闭，本条订阅已停用")
    return await bot.send("".join(lines))


@sv_crazy.on_fullmatch(("开启催命模式", "打开催命模式"), block=True)
async def open_crazy(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)
    if not cfg.crazy_enabled():
        return await bot.send("❌ 管理员已关闭催命模式总开关")

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None or not record.is_enabled:
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


@sv_threshold.on_regex(r"^(?:设置)?提醒阈值\s*(?P<num>\d{1,3})$", block=True)
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
    if record is None or not record.is_enabled:
        return await bot.send(
            f"❌ 请先发送 [{xw_bridge.xw_prefix()}开启体力提醒] 再设置阈值"
        )

    cur, refresh_ts, fetch_err = await xw_bridge.fetch_energy(uid, user_id, ev.bot_id)
    if cur is None:
        return await bot.send(f"❌ 设置失败：{fetch_err}")

    await WavesStaminaNotice.set_fields(
        user_id,
        ev.bot_id,
        uid,
        threshold=value,
        last_energy=cur,
        next_check_ts=compute_next_check(
            int(time.time()), cur, value, refresh_ts or 0
        ),
        last_error="",
    )
    logger.info(f"[体力提醒] 设置阈值 uid={uid} threshold={value}")
    return await bot.send(f"✅ 体力提醒阈值已设置为 {value}（当前体力 {cur}）")


@sv_threshold.on_fullmatch(
    ("设置提醒阈值", "提醒阈值", "查看提醒阈值", "查看体力提醒", "体力提醒状态"),
    block=True,
)
async def show_status(bot: Bot, ev: Event):
    user_id, uid, err = await _prepare(ev)
    if err:
        return await bot.send(err)

    record = await WavesStaminaNotice.get_record(user_id, ev.bot_id, uid)
    if record is None or not record.is_enabled:
        return await bot.send(
            f"❌ 当前未开启体力提醒\n"
            f"🔧 发送 [{xw_bridge.xw_prefix()}开启体力提醒] 即可开启"
        )

    next_ts = record.next_check_ts or 0
    next_text = (
        time.strftime("%m-%d %H:%M", time.localtime(next_ts))
        if next_ts > time.time()
        else "即将检查"
    )
    lines = [
        "📋 体力提醒状态\n",
        _targets_text(record) + "\n",
        f"🕒 阈值：{record.threshold}\n",
        f"🔥 催命模式：{'开启' if record.crazy == 'on' else '关闭'}\n",
        f"⏱ 下次检查：{next_text}\n",
        f"📊 累计推送：{record.push_count} 次",
    ]
    if record.last_error:
        lines.append(f"\n⚠️ 上次取数错误：{record.last_error}")
    return await bot.send("".join(lines))
