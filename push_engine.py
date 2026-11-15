"""提醒引擎：到点采样 -> 逐目标判定 -> 推送 -> 重算下次检查时间。

判定模型
--------
* 数据库里一行 = 一个推送目标（私聊一行、每个群各一行），阈值/催命/计时
  都按行走，所以「群里改了阈值」不影响私聊那行。
* 同一个 UID 的多行共用**一次** API 采样：本轮有任何一行到期，就按
  ``(user_id, bot_id, uid)`` 聚合，取一次体力后批量更新该 UID 的全部行。
  因此 6 分钟一轮的真实接口调用量与订阅人数相关，而不是与「私聊+群」的
  行数相关。
* 体力未达阈值：用服务端 refreshTimeStamp（体力回满时刻）反推「刚好到阈值」
  的时刻，把 next_check_ts 精确钉过去，中间不做无意义轮询。
* 体力已达阈值（普通模式）：靠 last_energy 的「低 -> 高」跃迁判定，只推一次；
  采样到体力回落后清零 last_push_ts，下次再满时会重新提醒。
* 催命模式：体力满之后按 CrazyMinutes 反复提醒，直到清体力或关闭催命。
"""

import asyncio
import time
from typing import Dict, List, Tuple

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger

from . import stamina_config as cfg
from .models import WavesStaminaNotice
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
    return "".join(lines)


async def _decide_row(record: WavesStaminaNotice, cur: int, refresh_ts: int, now: int) -> dict:
    """针对单行算出要写回的字段（含是否已推送）。"""
    threshold = record.threshold or CAP_DEFAULT
    crazy = record.crazy == "on" and cfg.crazy_enabled()
    updates: dict = {"last_energy": cur, "last_error": "", "fail_count": 0}

    if cur < threshold:
        # 体力已回落（用户清过体力）→ 允许下次满体力时重新提醒
        updates["last_push_ts"] = 0
        updates["next_check_ts"] = compute_next_check(now, cur, threshold, refresh_ts or 0)
        return updates

    if crazy:
        gap = cfg.crazy_minutes() * 60
        if now - (record.last_push_ts or 0) >= max(gap - 30, 0):
            if await send_push(record, _build_text(record, cur, threshold, True)):
                updates["last_push_ts"] = now
                updates["push_count"] = (record.push_count or 0) + 1
        updates["next_check_ts"] = now + gap
        return updates

    # 普通模式：只有「之前没推过」或「上次采样还在阈值下」才推
    last_push = record.last_push_ts or 0
    if last_push <= 0 or (record.last_energy or -1) < threshold:
        if await send_push(record, _build_text(record, cur, threshold, False)):
            updates["last_push_ts"] = now
            updates["push_count"] = (record.push_count or 0) + 1
    updates["next_check_ts"] = now + cfg.recheck_minutes() * 60
    return updates


async def _check_bucket(
    user_id: str,
    bot_id: str,
    uid: str,
    due_rows: List[WavesStaminaNotice],
    now: int,
) -> None:
    """一个 UID 的全部目标行，共用一次采样。"""
    cur, refresh_ts, err = await fetch_energy(uid, user_id, bot_id)
    rows = await WavesStaminaNotice.list_by_uid(user_id, bot_id, uid)
    if not rows:
        return

    # 采样失败：全部行一起退避，避免每轮都打接口
    if cur is None:
        for row in rows:
            await WavesStaminaNotice.set_fields(
                row.id,
                next_check_ts=now + FAIL_BACKOFF,
                fail_count=(row.fail_count or 0) + 1,
                last_error=(err or "")[:200],
            )
        logger.info(
            f"[鸣潮体力提醒] uid={uid} 采样失败（{len(due_rows)} 行待检查），"
            f"10 分钟后重试：{err}"
        )
        return

    pushed: List[str] = []
    for row in rows:
        try:
            updates = await _decide_row(row, cur, refresh_ts or 0, now)
        except Exception as e:
            logger.exception(f"[鸣潮体力提醒] 判定失败 uid={uid} row={row.id}: {e}")
            continue
        await WavesStaminaNotice.set_fields(row.id, **updates)
        if updates.get("last_push_ts") == now:
            pushed.append(row.target_label)

    if pushed:
        logger.info(
            f"[鸣潮体力提醒] 已推送 uid={uid} 体力={cur} 目标={'、'.join(pushed)}"
        )


async def tick() -> None:
    """一轮检查。由定时任务调用，也可手动触发（调试用）。"""
    if not cfg.push_enabled():
        return
    if _tick_lock.locked():
        logger.debug("[鸣潮体力提醒] 上一轮还没跑完，跳过本轮")
        return

    async with _tick_lock:
        now = int(time.time())
        try:
            rows = await WavesStaminaNotice.list_due(now)
        except Exception as e:
            logger.exception(f"[鸣潮体力提醒] 查询待检查记录失败: {e}")
            return
        if not rows:
            return

        # 同一 UID 的私聊行与群行合并成一次采样
        buckets: Dict[Tuple[str, str, str], List[WavesStaminaNotice]] = {}
        for row in rows:
            if not row.is_valid:
                continue
            key = (row.user_id, row.bot_id, row.uid)
            buckets.setdefault(key, []).append(row)

        if not buckets:
            return
        logger.debug(
            f"[鸣潮体力提醒] 本轮 {len(rows)} 行待检查，涉及 {len(buckets)} 个账号"
        )

        for (user_id, bot_id, uid), due_rows in buckets.items():
            try:
                await _check_bucket(user_id, bot_id, uid, due_rows, now)
            except Exception as e:
                logger.exception(f"[鸣潮体力提醒] 处理 uid={uid} 异常: {e}")
            await asyncio.sleep(0.3)


@scheduler.scheduled_job(
    "interval",
    minutes=cfg.check_minutes(),
    id="stamina_notice_push",
)
async def _scheduled_tick() -> None:
    """每6分钟检查体力是否达到设定阈值"""
    await tick()
