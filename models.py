"""StaminaNotice 自建数据表。

刻意不复用 XutheringWavesUID 的 WavesStaminaRecord：
那张表由 XutheringWavesUID 的「每日」和后台 Admin 写入，外部插件插写会与它的
upsert 抢行、语义也会串（它的 stamina_push_switch 既表示开关又表示群号）。

推送目标设计
------------
一个人可以**同时**在私聊和多个群里收提醒，所以目标不是互斥的单值：

* ``push_private`` —— 私聊推送开关（0/1）
* ``group_ids``    —— 群号列表，逗号分隔，支持一人多群

``push_target`` / ``group_id`` 是 v1 的单目标遗留字段，仅用于兼容旧行读取；
新写入不再依赖它们（改动时仍会一起置为 off，避免旧逻辑复活）。
"""

import time
from typing import Any, Dict, List, Optional, Type, TypeVar

from sqlmodel import Field, SQLModel, col, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text, update
from sqlalchemy.sql import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import (
    BaseModel,
    engine,
    with_session,
)

T_Notice = TypeVar("T_Notice", bound="WavesStaminaNotice")

# 遗留字段取值（仅兼容旧数据）
TARGET_OFF = "off"
TARGET_PRIVATE = "private"
TARGET_GROUP = "group"


def _split_groups(raw: str) -> List[str]:
    return [g.strip() for g in (raw or "").split(",") if g.strip()]


def _join_groups(groups: List[str]) -> str:
    seen: List[str] = []
    for g in groups:
        g = g.strip()
        if g and g not in seen:
            seen.append(g)
    return ",".join(seen)


class WavesStaminaNotice(BaseModel, table=True):
    """体力提醒订阅表。"""

    __tablename__ = "WavesStaminaNotice"
    __table_args__: Dict[str, Any] = {"extend_existing": True}

    uid: str = Field(default="", title="鸣潮UID", index=True)
    bot_self_id: str = Field(default="", title="BotSelfID")

    # —— 推送目标（可同时存在）——
    push_private: int = Field(default=0, title="私聊推送 0/1")
    group_ids: str = Field(default="", title="群推送群号列表(逗号分隔)")

    # —— 遗留单目标字段，只读兼容 ——
    push_target: str = Field(default=TARGET_OFF, title="[遗留] 单目标 off/private/group")
    group_id: str = Field(default="", title="[遗留] 单群号")

    threshold: int = Field(default=240, title="体力阈值")
    next_check_ts: int = Field(default=0, title="下次检查时间戳(0=立即)")
    last_energy: int = Field(default=-1, title="上次采样体力值")
    crazy: str = Field(default="off", title="催命模式 on/off")
    last_push_ts: int = Field(default=0, title="上次推送时间戳")
    push_count: int = Field(default=0, title="累计推送次数")
    fail_count: int = Field(default=0, title="连续取数失败次数")
    last_error: str = Field(default="", title="上次取数错误")
    created_time: Optional[int] = Field(default=None, title="创建时间")
    updated_time: Optional[int] = Field(default=None, title="更新时间")

    # ---------- 目标解析 ----------

    @property
    def private_on(self) -> bool:
        if self.push_private:
            return True
        return self.push_target == TARGET_PRIVATE

    @property
    def groups(self) -> List[str]:
        groups = _split_groups(self.group_ids)
        if groups:
            return groups
        if self.push_target == TARGET_GROUP and self.group_id:
            return [self.group_id]
        return []

    @property
    def is_enabled(self) -> bool:
        return self.private_on or bool(self.groups)

    def describe_targets(self) -> str:
        parts = []
        if self.private_on:
            parts.append("私聊")
        if self.groups:
            parts.append("群 " + "、".join(self.groups))
        return " / ".join(parts) if parts else "未开启"

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
        """取出所有「至少有一个推送目标」且已到检查时间的记录。"""
        sql = (
            select(cls)
            .where(
                and_(
                    or_(
                        cls.push_private == 1,
                        cls.group_ids != "",
                        cls.push_target == TARGET_PRIVATE,
                        cls.push_target == TARGET_GROUP,
                    ),
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

    @classmethod
    async def add_group(
        cls: Type[T_Notice],
        user_id: str,
        bot_id: str,
        uid: str,
        group_id: str,
        bot_self_id: str = "",
    ) -> bool:
        """把某个群加入该用户的推送目标（幂等）。"""
        record = await cls.get_record(user_id, bot_id, uid)
        if record is None:
            return False
        await cls.set_fields(
            user_id,
            bot_id,
            uid,
            group_ids=_join_groups(record.groups + [group_id]),
            push_private=1 if record.private_on else 0,
            push_target=TARGET_OFF,
            group_id="",
            bot_self_id=bot_self_id or record.bot_self_id,
        )
        return True

    @classmethod
    async def remove_group(
        cls: Type[T_Notice],
        user_id: str,
        bot_id: str,
        uid: str,
        group_id: str,
    ) -> bool:
        """把某个群从推送目标里移除（幂等）。"""
        record = await cls.get_record(user_id, bot_id, uid)
        if record is None:
            return False
        groups = [g for g in record.groups if g != group_id]
        await cls.set_fields(
            user_id,
            bot_id,
            uid,
            group_ids=_join_groups(groups),
            push_private=1 if record.private_on else 0,
            push_target=TARGET_OFF,
            group_id="",
        )
        return True


def _default_clause(column) -> Optional[str]:
    server_default = column.server_default
    if server_default is not None:
        arg = getattr(server_default, "arg", None)
        text_val = getattr(arg, "text", None)
        if text_val is not None:
            return text_val
        if arg is not None:
            return str(arg)
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
    return None


async def _add_missing_columns(table) -> None:
    """老表补列（热重载 / 升级场景），幂等。"""

    def _existing(sync_conn, name):
        insp = sa_inspect(sync_conn)
        if not insp.has_table(name):
            return None
        return {c["name"] for c in insp.get_columns(name)}

    async with engine.connect() as conn:
        existing = await conn.run_sync(_existing, table.name)
        dialect = conn.dialect
    if existing is None:
        return

    for column in table.columns:
        if column.name in existing:
            continue
        sql = (
            f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
            f"{column.type.compile(dialect=dialect)}"
        )
        default_clause = _default_clause(column)
        if default_clause is not None:
            sql += f" DEFAULT {default_clause}"
            if not column.nullable:
                sql += " NOT NULL"
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
            logger.info(f"[体力提醒] 补列 {sql}")
        except Exception as e:
            logger.warning(f"[体力提醒] 补列失败 {sql} -> {e}")


async def _backfill_targets() -> None:
    """把 v1 的单目标数据迁移到新字段（幂等）。"""
    table = WavesStaminaNotice.__tablename__
    statements = (
        f"UPDATE {table} SET push_private = 1 "
        f"WHERE push_target = '{TARGET_PRIVATE}' AND push_private = 0",
        f"UPDATE {table} SET group_ids = group_id "
        f"WHERE push_target = '{TARGET_GROUP}' AND (group_ids IS NULL OR group_ids = '') "
        f"AND group_id IS NOT NULL AND group_id != ''",
    )
    for sql in statements:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception as e:
            logger.debug(f"[体力提醒] 迁移旧目标跳过: {e}")


async def ensure_tables() -> None:
    """建表 + 补列 + 旧数据迁移（全部幂等）。"""
    table = WavesStaminaNotice.__table__  # type: ignore[attr-defined]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=[table]))
    await _add_missing_columns(table)
    await _backfill_targets()


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
