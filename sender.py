"""推送发送器。

只推给「订阅时记录下来的那一个 bot」，而不是像某些实现那样遍历
``gss.active_bot`` 全量广播——多 bot 场景下那样会重复发 N 份。
"""

import asyncio
import random

from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment

from .models import TARGET_GROUP, WavesStaminaNotice


def _pick_bot(bot_id: str):
    active = getattr(gss, "active_bot", None)
    if not active:
        return None
    try:
        return active.get(bot_id) or next(iter(active.values()))
    except Exception:
        return None


async def send_push(record: WavesStaminaNotice, text: str) -> bool:
    """按记录里的目标投递一条文本提醒。"""
    bot = _pick_bot(record.bot_id)
    if bot is None:
        logger.warning(f"[体力提醒] 没有可用的 bot，跳过推送 uid={record.uid}")
        return False

    if record.push_target == TARGET_GROUP and record.group_id:
        target_type = "group"
        target_id = record.group_id
        messages = [MessageSegment.text(text), MessageSegment.at(record.user_id)]
    else:
        target_type = "direct"
        target_id = record.user_id
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
        await asyncio.sleep(0.5 + random.random())
        return True
    except Exception as e:
        logger.exception(f"[体力提醒] 推送失败 uid={record.uid} target={target_type}:{target_id} -> {e}")
        return False
