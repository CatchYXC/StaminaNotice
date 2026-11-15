"""推送发送器（一行只发一个目标）。

数据层是「一个目标一行」，所以这里不再展开目标列表：
* ``target_type == 'private'`` → 发到 ``user_id``，纯文本
* ``target_type == 'group'``   → 发到 ``target_id``（群号），文本 + @订阅者本人

只推给「订阅时记录下来的那一个 bot」，不像某些实现那样遍历 ``gss.active_bot``
全量广播——多 bot 场景下那样会重复发 N 份。
"""

import asyncio
import random
from typing import List, Tuple

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


def _target(record: WavesStaminaNotice) -> Tuple[str, str]:
    """这一行要发到哪：(direct|group, 目标ID)。"""
    if record.target_type == TARGET_GROUP and record.target_id:
        return "group", record.target_id
    return "direct", record.user_id


async def send_push(record: WavesStaminaNotice, text: str) -> bool:
    """把提醒发到这一行对应的目标，成功返回 True。"""
    target_type, target_id = _target(record)
    if not target_id:
        logger.warning(f"[鸣潮体力提醒] 目标为空，跳过 uid={record.uid} id={record.id}")
        return False

    bot = _pick_bot(record.bot_id)
    if bot is None:
        logger.warning(f"[鸣潮体力提醒] 没有可用的 bot，跳过推送 uid={record.uid}")
        return False

    if target_type == "group":
        messages: List = [
            MessageSegment.text(text),
            MessageSegment.at(record.user_id),
        ]
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
        await asyncio.sleep(0.5 + random.random())
        return True
    except Exception as e:
        logger.exception(
            f"[鸣潮体力提醒] 推送失败 uid={record.uid} "
            f"target={target_type}:{target_id} -> {e}"
        )
        return False
