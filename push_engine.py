"""提醒引擎：到点采样 -> 判定 -> 推送 -> 重算下次检查时间。

判定模型
--------
* 每次只对「next_check_ts <= now」的记录做一次真实 API 采样，其余记录只走一条
  数据库查询，所以 6 分钟一轮的成本很低。
* 体力未达阈值：直接用服务端给的 refreshTimeStamp（体力回满时刻）反推
  「体力刚好到阈值」的时刻，把 next_check_ts 精确钉过去，中间不做无意义轮询。
* 体力已达阈值（普通模式）：靠 last_energy 的「低 -> 高」跃迁判定，只推一次；
  之后按 RecheckMinutes 重新采样，用于发现用户已经清完体力。
* 催命模式：体力满之后按 CrazyMinutes 反复提醒，直到用户清体力或关闭催命。
"""

import asyncio
import time

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger

from . import stamina_config as cfg
from .models import TARGET_OFF, WavesStaminaNotice
from .sender import send_push
from .xw_bridge import fetch_energy, xw_prefix

POINT_SECONDS = 360  # 鸣潮体力：每 6 分钟恢复 1 点
CAP_DEFAULT = 240
FAIL_BACKOFF = 600  # 取数失败后 10 分钟重试

_tick_lock = asyncio.Lock()


def compute_next_check(
    now: int,
    cur: int,
    threshold: int,
    refresh_ts: int = 0,
    cap: int = CAP_DEFAULT,
) -> int:
    """算出「体力刚好到达阈值」的时间戳。"""
    if refresh_ts and refresh_ts > now and cap > threshold:
        target = refresh_ts - (cap - threshold) * POINT_SECONDS
        return max(target, now + POINT_SECONDS)
    if cur < threshold:
        return now + max(POINT_SECONDS, (threshold - cur) * POINT_SECONDS)
    return now + POINT_SECONDS


def _build_text(record: WavesStaminaNotice, cur: int, threshold: int, crazy: bool) -> str:
    prefix = xw_prefix()
    head = "🔥[鸣潮] 催命提醒" if crazy else "✅[鸣潮] 体力提醒"
    lines = [
        f"{head}\n",
        f"🌜您的结晶波片已达到设定阈值（UID:{record.uid}）\n",
        f"🕒当前体力：{cur} / 阈值：{threshold}\n",
    ]
    if crazy:
        lines.append(f"⏰催命模式开启中，{cfg.crazy_minutes()} 分钟后仍未清理会再次提醒\n")
    else:
        lines.append("📅清完体力后会自动重新计时\n")
    lines.append(f"🔧发送 [{prefix}关闭体力提醒] 可随时关闭")
    return "".join(lines)


async def _check_one(record: WavesStaminaNotice, now: int) -> None:
    cur, refresh_ts, err = await fetch_energy(
        record.uid, record.user_id, record.bot_id
    )

    if cur is None:
        await WavesStaminaNotice.set_fields(
            record.user_id,
            record.bot_id,
            record.uid,
            next_check_ts=now + FAIL_BACKOFF,
            last_error=err[:200],
        )
        logger.info(f"[体力提醒] uid={record.uid} 采样失败，10 分钟后重试：{err}")
        return

    threshold = record.threshold or CAP_DEFAULT
    crazy = record.crazy == "on" and cfg.crazy_enabled()
    updates = {"last_energy": cur, "last_error": ""}

    if cur >= threshold:
        if crazy:
            gap = cfg.crazy_minutes() * 60
            if now - record.last_push_ts >= gap - 30:
                if await send_push(record, _build_text(record, cur, threshold, True)):
                    updates["last_push_ts"] = now
                    updates["push_count"] = (record.push_count or 0) + 1
            updates["next_check_ts"] = now + gap
        else:
            if record.last_energy < 0 or record.last_energy < threshold:
                if await send_push(record, _build_text(record, cur, threshold, False)):
                    updates["last_push_ts"] = now
                    updates["push_count"] = (record.push_count or 0) + 1
            updates["next_check_ts"] = now + cfg.recheck_minutes() * 60
    else:
        updates["next_check_ts"] = compute_next_check(
            now, cur, threshold, refresh_ts or 0
        )

    await WavesStaminaNotice.set_fields(
        record.user_id, record.bot_id, record.uid, **updates
    )
    if updates.get("last_push_ts") == now:
        logger.info(
            f"[体力提醒] 已推送给 uid={record.uid} target={record.push_target} 体力={cur}"
        )


async def tick() -> None:
    """一轮检查。由定时任务调用，也可手动触发（调试用）。"""
    if not cfg.push_enabled():
        return
    if _tick_lock.locked():
        logger.debug("[体力提醒] 上一轮还没跑完，跳过本轮")
        return

    async with _tick_lock:
        now = int(time.time())
        try:
            records = await WavesStaminaNotice.list_due(now)
        except Exception as e:
            logger.exception(f"[体力提醒] 查询待推送记录失败: {e}")
            return
        if not records:
            return

        logger.debug(f"[体力提醒] 本轮待检查 {len(records)} 条")
        for record in records:
            if record.push_target == TARGET_OFF:
                continue
            try:
                await _check_one(record, now)
            except Exception as e:
                logger.exception(f"[体力提醒] 处理 uid={record.uid} 异常: {e}")
            await asyncio.sleep(0.3)


@scheduler.scheduled_job(
    "interval",
    minutes=cfg.check_minutes(),
    id="stamina_notice_push",
)
async def _scheduled_tick() -> None:
    await tick()
