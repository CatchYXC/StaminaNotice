"""推送发送器（支持一次投递多个目标）。

同一个人可以同时订阅「私聊」和「若干个群」，所以这里按目标列表逐个发送：
- 私聊：纯文本
- 群聊：文本 + @订阅者本人（不是 @全体）

只推给「订阅时记录下来的那一个 bot」，不像某些实现那样遍历 ``gss.active_bot``
全量广播——多 bot 场景下那样会重复发 N 份。
"""

import asyncio
import random
from typing import List, Tuple

from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment

from .models import WavesStaminaNotice


def _pick_bot(bot_id: str):
    active = getattr(gss, "active_bot", None)
    if not active:
        return None
    try:
        return active.get(bot_id) or next(iter(active.values()))
    except Exception:
        return None


def _targets(record: WavesStaminaNotice) -> List[Tuple[str, str]]:
    """展开推送目标：[(direct|group, 目标ID), ...]"""
    targets: List[Tuple[str, str]] = []
    if record.private_on and record.user_id:
        targets.append(("direct", record.user_id))
    for group_id in record.groups:
        if group_id:
            targets.append(("group", group_id))
    return targets


async def send_push(record: WavesStaminaNotice, text: str) -> int:
    """按记录里的所有目标投递，返回成功条数。"""
    targets = _targets(record)
    if not targets:
        return 0

    bot = _pick_bot(record.bot_id)
    if bot is None:
        logger.warning(f"[体力提醒] 没有可用的 bot，跳过推送 uid={record.uid}")
        return 0

    ok = 0
    for target_type, target_id in targets:
        if target_type == "group":
            messages = [MessageSegment.text(text), MessageSegment.at(record.user_id)]
        else:
            messages = [MessageSegment.text(text)]
        try:
            await bot.target_send(
                messages,
                target_type,
                target_id,
                record.bot_id,
                record.bot_self_id,
                "",
            )
            ok += 1
        except Exception as e:
            logger.exception(
                f"[体力提醒] 推送失败 uid={record.uid} "
                f"target={target_type}:{target_id} -> {e}"
            )
        await asyncio.sleep(0.5 + random.random())
    return ok
