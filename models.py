"""StaminaNotice 自建数据表。

刻意不复用 XutheringWavesUID 的 WavesStaminaRecord：
那张表由 XutheringWavesUID 的「每日」和后台 Admin 写入，外部插件插写会与它的
upsert 抢行、语义也会串（它的 stamina_push_switch 既表示开关又表示群号）。
这里用独立的 WavesStaminaNotice 表，主键维度为 user_id + bot_id + uid。
"""

import time
from typing import Any, Dict, List, Optional, Type, TypeVar

from sqlmodel import Field, SQLModel, col, select
from sqlalchemy import update
from sqlalchemy.sql import and_
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import (
    BaseModel,
    engine,
    with_session,
)

T_Notice = TypeVar("T_Notice", bound="WavesStaminaNotice")

TARGET_OFF = "off"
TARGET_PRIVATE = "private"
TARGET_GROUP = "group"


class WavesStaminaNotice(BaseModel, table=True):
    """体力提醒订阅表。"""

    __tablename__ = "WavesStaminaNotice"
    __table_args__: Dict[str, Any] = {"extend_existing": True}

    uid: str = Field(default="", title="鸣潮UID", index=True)
    bot_self_id: str = Field(default="", title="BotSelfID")
    push_target: str = Field(default=TARGET_OFF, title="推送目标: off/private/group")
    group_id: str = Field(default="", title="推送群号")
    threshold: int = Field(default=240, title="体力阈值")
    next_check_ts: int = Field(default=0, title="下次检查时间戳(0=立即)")
    last_energy: int = Field(default=-1, title="上次采样体力值")
    crazy: str = Field(default="off", title="催命模式 on/off")
    last_push_ts: int = Field(default=0, title="上次推送时间戳")
    push_count: int = Field(default=0, title="累计推送次数")
    last_error: str = Field(default="", title="上次取数错误")
    created_time: Optional[int] = Field(default=None, title="创建时间")
    updated_time: Optional[int] = Field(default=None, title="更新时间")

    # ---------- 查询 ----------

    @classmethod
    @with_session
    async def get_record(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
    ) -> Optional[T_Notice]:
        sql = select(cls).where(
            and_(
                cls.user_id == user_id,
                cls.bot_id == bot_id,
                cls.uid == uid,
            )
        )
        result = await session.execute(sql)
        return result.scalars().first()

    @classmethod
    @with_session
    async def list_due(
        cls: Type[T_Notice],
        session: AsyncSession,
        now: int,
        limit: int = 300,
    ) -> List[T_Notice]:
        """取出所有开启推送且已到检查时间的记录。"""
        sql = (
            select(cls)
            .where(
                and_(
                    cls.push_target != TARGET_OFF,
                    cls.next_check_ts <= now,
                )
            )
            .limit(limit)
        )
        result = await session.execute(sql)
        return list(result.scalars().all())

    # ---------- 写入 ----------

    @classmethod
    @with_session
    async def upsert(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
        bot_self_id: str = "",
        **fields: Any,
    ) -> T_Notice:
        """不存在则建行，存在则更新传入字段。"""
        sql = select(cls).where(
            and_(
                cls.user_id == user_id,
                cls.bot_id == bot_id,
                cls.uid == uid,
            )
        )
        result = await session.execute(sql)
        record = result.scalars().first()
        now = int(time.time())

        if record is None:
            record = cls(
                user_id=user_id,
                bot_id=bot_id,
                uid=uid,
                bot_self_id=bot_self_id,
                created_time=now,
                updated_time=now,
            )
            session.add(record)
        for key, value in fields.items():
            if hasattr(record, key) and value is not None:
                setattr(record, key, value)
        if bot_self_id:
            record.bot_self_id = bot_self_id
        record.updated_time = now

        await session.commit()
        await session.refresh(record)
        return record

    @classmethod
    @with_session
    async def set_fields(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
        **fields: Any,
    ) -> None:
        """只更新字段，不建行（定时任务用，行已被删除时静默跳过）。"""
        values = {k: v for k, v in fields.items() if v is not None}
        if not values:
            return
        values["updated_time"] = int(time.time())
        sql = (
            update(cls)
            .where(
                and_(
                    col(cls.user_id) == user_id,
                    col(cls.bot_id) == bot_id,
                    col(cls.uid) == uid,
                )
            )
            .values(**values)
        )
        await session.execute(sql)
        await session.commit()


async def ensure_tables() -> None:
    """建表（幂等）。只建本插件自己的表，不触碰其它插件的 metadata。"""
    tables = [WavesStaminaNotice.__table__]  # type: ignore[attr-defined]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=tables))


# 后台管理页（可选，失败不影响功能）
try:
    from gsuid_core.webconsole.mount_app import (
        PageSchema,
        GsAdminModel,
        site,
    )

    @site.register_admin
    class WavesStaminaNoticeAdmin(GsAdminModel):
        pk_name = "id"
        page_schema = PageSchema(
            label="鸣潮体力提醒",
            icon="fa fa-bell",
        )  # type: ignore

        model = WavesStaminaNotice

except Exception:  # pragma: no cover - 老版本 gsuid_core 没有 webconsole
    logger.debug("[体力提醒] 未注册后台管理页")
