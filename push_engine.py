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
from typing import Dict, List, Optional, Tuple

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger

from . import stamina_config as cfg
from .models import WavesStaminaNotice
from .sender import send_push
from .xw_bridge import StaminaSnapshot, fetch_daily_image, fetch_snapshot

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


def _build_text(
    record: WavesStaminaNotice, snap: StaminaSnapshot, threshold: int, crazy: bool
) -> str:
    head = "🔥[鸣潮] 催命提醒" if crazy else "✅[鸣潮] 体力提醒"
    lines = [
        f"{head}\n",
        f"🌜您的结晶波片已达到设定阈值（UID:{record.uid}）\n",
        f"🕒当前体力：{snap.cur} / 阈值：{threshold}\n",
    ]
    if crazy:
        lines.append(f"⏰催命模式开启中，{cfg.crazy_minutes()} 分钟后仍未清理会再次提醒\n")
    else:
        lines.append("📅清完体力后会自动重新计时\n")
    return "".join(lines)


async def _fetch_image(record: WavesStaminaNotice) -> Tuple[Optional[bytes], str]:
    """取一张「每日」的体力图；没开图片开关时返回 ``(None, "")``。

    整条链路上任何一环出问题都只降级成文字，绝不让推送本身失败。
    """
    if not cfg.push_image():
        return None, ""
    try:
        return await fetch_daily_image(
            record.uid,
            record.user_id,
            record.bot_id,
            record.bot_self_id or "",
        )
    except Exception as e:
        logger.exception(f"[鸣潮体力提醒] 取每日图片异常 uid={record.uid}: {e}")
        return None, f"{type(e).__name__}: {e}"[:120]


async def _push_row(
    record: WavesStaminaNotice, snap: StaminaSnapshot, threshold: int, crazy: bool
) -> bool:
    """发一条提醒：配「每日」那张图，图出不来就只发文字。"""
    text = _build_text(record, snap, threshold, crazy)
    image, _ = await _fetch_image(record)
    return await send_push(record, text, image)


def _build_test_text(
    record: WavesStaminaNotice,
    snap: StaminaSnapshot,
    threshold: int,
    has_image: bool,
    err: str = "",
) -> str:
    """测试推送的伴随文字：说明是调试消息，并交代图片有没有出来。"""
    lines = [
        "🧪[鸣潮] 测试推送\n",
        f"🌜DEBUG 用的预览消息，可直接忽略（UID:{record.uid}）\n",
        f"🕒当前结晶波片：{snap.cur} / {snap.total or CAP_DEFAULT}，阈值：{threshold}\n",
        "📅本条不写入计时、不计入推送次数，正式提醒照旧\n",
    ]
    if cfg.push_image() and not has_image:
        detail = f"：{err}" if err else "（原因见核心日志）"
        lines.append(f"⚠️每日图片没拿到，本条已退回纯文字{detail}\n")
    return "".join(lines)


async def push_test(
    record: WavesStaminaNotice,
    snap: StaminaSnapshot,
    threshold: int,
) -> bool:
    """调试用：立刻按正式样式发一条，**不触碰数据库**。

    ``record`` 是内存里临时拼的行（没有主键），只用来确定投递目标；
    和正式推送共用 ``send_push`` + 取图链路，所以能完整验证
    「图能不能出来、群里会不会 @ 到人」。
    """
    image, err = await _fetch_image(record)
    ok = await send_push(
        record, _build_test_text(record, snap, threshold, bool(image), err), image
    )
    logger.info(
        f"[鸣潮体力提醒][调试] 测试推送 uid={record.uid} "
        f"target={record.target_type}:{record.target_id or '-'} "
        f"体力={snap.cur}/{threshold} 图片={'是' if image else '否'} 结果={'成功' if ok else '失败'}"
    )
    return ok


async def _decide_row(
    record: WavesStaminaNotice, snap: StaminaSnapshot, now: int
) -> dict:
    """针对单行算出要写回的字段（含是否已推送）。"""
    cur = snap.cur
    refresh_ts = snap.refresh_ts or 0
    threshold = record.threshold or CAP_DEFAULT
    crazy = record.crazy == "on" and cfg.crazy_enabled()
    updates: dict = {"last_energy": cur, "last_error": "", "fail_count": 0}

    if cur < threshold:
        # 体力已回落（用户清过体力）→ 允许下次满体力时重新提醒
        updates["last_push_ts"] = 0
        updates["next_check_ts"] = compute_next_check(now, cur, threshold, refresh_ts)
        return updates

    if crazy:
        gap = cfg.crazy_minutes() * 60
        if now - (record.last_push_ts or 0) >= max(gap - 30, 0):
            if await _push_row(record, snap, threshold, True):
                updates["last_push_ts"] = now
                updates["push_count"] = (record.push_count or 0) + 1
        updates["next_check_ts"] = now + gap
        return updates

    # 普通模式：只有「之前没推过」或「上次采样还在阈值下」才推
    last_push = record.last_push_ts or 0
    if last_push <= 0 or (record.last_energy or -1) < threshold:
        if await _push_row(record, snap, threshold, False):
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
    snap, err = await fetch_snapshot(uid, user_id, bot_id)
    rows = await WavesStaminaNotice.list_by_uid(user_id, bot_id, uid)
    if not rows:
        return

    # 采样失败：全部行一起退避，避免每轮都打接口
    if snap is None:
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

    cur = snap.cur
    pushed: List[str] = []
    for row in rows:
        try:
            updates = await _decide_row(row, snap, now)
        except Exception as e:
            logger.exception(f"[鸣潮体力提醒] 判定失败 uid={uid} row={row.id}: {e}")
            continue
        if cfg.debug_enabled():
            logger.info(
                f"[鸣潮体力提醒][调试] uid={uid} row={row.id} "
                f"{row.target_type}:{row.target_id or '-'} "
                f"体力={cur}/{snap.total} 阈值={row.threshold} "
                f"上次采样={row.last_energy} 上次推送={row.last_push_ts or 0} "
                f"→ 写回 {updates}"
            )
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


async def _scheduled_tick() -> None:
    """每6分钟检查体力是否达到设定阈值"""
    await tick()


def _register_job() -> None:
    """注册定时任务。

    这里故意不用 ``@scheduler.scheduled_job(id=...)`` 装饰器：
    那个写法在**插件热重载**时会因为 job id 重复抛 ``ConflictingIdError``，
    而 ``push_engine`` 在 ``__init__.py`` 里比 ``commands`` 先导入，
    一旦这里抛异常，后面的指令模块就整个不会被加载 —— 表现就是「所有指令都没反应」。
    改成 ``add_job(replace_existing=True)`` 并捕获异常，重载多少次都安全。
    """
    try:
        scheduler.add_job(
            _scheduled_tick,
            "interval",
            minutes=cfg.check_minutes(),
            id="stamina_notice_push",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.debug(
            f"[鸣潮体力提醒] 定时任务已注册（每 {cfg.check_minutes()} 分钟）"
        )
    except Exception as e:
        logger.warning(
            f"[鸣潮体力提醒] 注册定时任务失败，提醒将不会自动触发：{type(e).__name__}: {e}"
        )


_register_job()
