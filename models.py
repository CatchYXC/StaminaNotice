"""StaminaNotice 自建数据表（一行 = 一个推送目标）。

数据模型
--------
唯一键：``(user_id, bot_id, uid, target_type, target_id)``

"""

import time
from typing import Any, Dict, List, Optional, Type, TypeVar

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import and_, func
from sqlmodel import Field, SQLModel, col, select

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import (
    BaseModel,
    engine,
    with_session,
)

T_Notice = TypeVar("T_Notice", bound="WavesStaminaNotice")

TARGET_PRIVATE = "private"
TARGET_GROUP = "group"
TARGET_TYPES = (TARGET_PRIVATE, TARGET_GROUP)


class WavesStaminaNotice(BaseModel, table=True):
    """体力提醒订阅表：一行 = 一个推送目标。"""

    __tablename__ = "WavesStaminaNotice"
    __table_args__: Dict[str, Any] = {"extend_existing": True}

    uid: str = Field(default="", title="鸣潮UID", index=True)
    bot_self_id: str = Field(default="", title="BotSelfID")

    # —— 推送目标：一个目标一行 ——
    target_type: str = Field(default="", title="推送类型")
    target_id: str = Field(default="", title="群号")

    # —— 每个目标各自独立的设置与状态 ——
    threshold: int = Field(default=240, title="体力阈值")
    next_check_ts: int = Field(default=0, title="下次检查时间")
    last_energy: int = Field(default=-1, title="结晶波片值")
    crazy: str = Field(default="off", title="催命模式")
    last_push_ts: int = Field(default=0, title="上次推送时间")
    push_count: int = Field(default=0, title="累计推送次数")
    fail_count: int = Field(default=0, title="连续取数失败次数")
    last_error: str = Field(default="", title="上次取数错误")
    created_time: Optional[int] = Field(default=None, title="创建时间")
    updated_time: Optional[int] = Field(default=None, title="更新时间")

    # ---------- 展示 ----------

    @property
    def is_group(self) -> bool:
        return self.target_type == TARGET_GROUP

    @property
    def is_valid(self) -> bool:
        """行是否还是一个有效的推送目标。"""
        if self.target_type == TARGET_PRIVATE:
            return bool(self.user_id)
        if self.target_type == TARGET_GROUP:
            return bool(self.target_id)
        return False

    @property
    def target_label(self) -> str:
        if self.is_group:
            return f"群 {self.target_id}"
        return "私聊"

    # ---------- 查询 ----------

    @classmethod
    @with_session
    async def get_row(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
        target_type: str,
        target_id: str = "",
    ) -> Optional[T_Notice]:
        """取某个人在某个目标上的那一行。"""
        sql = select(cls).where(
            and_(
                col(cls.user_id) == user_id,
                col(cls.bot_id) == bot_id,
                col(cls.uid) == uid,
                col(cls.target_type) == target_type,
                func.coalesce(col(cls.target_id), "") == (target_id or ""),
            )
        )
        result = await session.execute(sql)
        return result.scalars().first()

    @classmethod
    @with_session
    async def list_rows(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
    ) -> List[T_Notice]:
        """取某个人的全部目标行。"""
        sql = (
            select(cls)
            .where(
                and_(
                    col(cls.user_id) == user_id,
                    col(cls.bot_id) == bot_id,
                    col(cls.uid) == uid,
                )
            )
            .order_by(col(cls.target_type), col(cls.target_id))
        )
        result = await session.execute(sql)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def list_by_uid(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
    ) -> List[T_Notice]:
        """同 list_rows，供定时任务在共享一次 API 采样后批量更新。"""
        sql = select(cls).where(
            and_(
                col(cls.user_id) == user_id,
                col(cls.bot_id) == bot_id,
                col(cls.uid) == uid,
            )
        )
        result = await session.execute(sql)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def list_due(
        cls: Type[T_Notice],
        session: AsyncSession,
        now: int,
        limit: int = 500,
    ) -> List[T_Notice]:
        """取出所有已到检查时间的行。"""
        sql = (
            select(cls)
            .where(
                and_(
                    col(cls.target_type).in_(TARGET_TYPES),
                    col(cls.next_check_ts) <= now,
                )
            )
            .limit(limit)
        )
        result = await session.execute(sql)
        return list(result.scalars().all())

    # ---------- 写入 ----------

    @classmethod
    @with_session
    async def upsert_row(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
        target_type: str,
        target_id: str = "",
        bot_self_id: str = "",
        **fields: Any,
    ) -> T_Notice:
        """在该目标上建行或更新字段。"""
        if target_type not in TARGET_TYPES:
            raise ValueError(f"非法 target_type: {target_type}")

        sql = select(cls).where(
            and_(
                col(cls.user_id) == user_id,
                col(cls.bot_id) == bot_id,
                col(cls.uid) == uid,
                col(cls.target_type) == target_type,
                func.coalesce(col(cls.target_id), "") == (target_id or ""),
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
                target_type=target_type,
                target_id=target_id or "",
                bot_self_id=bot_self_id or "",
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
        row_id: Optional[int],
        **fields: Any,
    ) -> None:
        """按主键更新字段。"""
        if not row_id:
            return
        values = {k: v for k, v in fields.items() if v is not None}
        if not values:
            return
        values["updated_time"] = int(time.time())
        sql = update(cls).where(col(cls.id) == row_id).values(**values)
        await session.execute(sql)
        await session.commit()

    @classmethod
    @with_session
    async def delete_row(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
        target_type: str,
        target_id: str = "",
    ) -> bool:
        """删除某个目标上的那一行，返回是否真的删掉了。"""
        sql = select(cls).where(
            and_(
                col(cls.user_id) == user_id,
                col(cls.bot_id) == bot_id,
                col(cls.uid) == uid,
                col(cls.target_type) == target_type,
                func.coalesce(col(cls.target_id), "") == (target_id or ""),
            )
        )
        result = await session.execute(sql)
        rows = list(result.scalars().all())
        for row in rows:
            await session.delete(row)
        await session.commit()
        return bool(rows)

    @classmethod
    @with_session
    async def delete_all(
        cls: Type[T_Notice],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
    ) -> int:
        """删除某人某个 UID 下的全部目标行。"""
        sql = select(cls).where(
            and_(
                col(cls.user_id) == user_id,
                col(cls.bot_id) == bot_id,
                col(cls.uid) == uid,
            )
        )
        result = await session.execute(sql)
        rows = list(result.scalars().all())
        for row in rows:
            await session.delete(row)
        await session.commit()
        return len(rows)


# ---------------------------- 建表 / 迁移 ----------------------------


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


def _table_columns(sync_conn, name: str) -> Optional[List[str]]:
    insp = sa_inspect(sync_conn)
    if not insp.has_table(name):
        return None
    return [c["name"] for c in insp.get_columns(name)]


async def _add_missing_columns(table) -> None:
    """老表补列，幂等。"""
    async with engine.connect() as conn:
        existing = await conn.run_sync(_table_columns, table.name)
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
            logger.info(f"[鸣潮体力提醒] 补列 {sql}")
        except Exception as e:
            logger.warning(f"[鸣潮体力提醒] 补列失败 {sql} -> {e}")


_LEGACY_COLUMNS = ("push_private", "group_ids", "push_target", "group_id")
_CARRY_COLUMNS = (
    "bot_self_id",
    "threshold",
    "next_check_ts",
    "last_energy",
    "crazy",
    "last_push_ts",
    "push_count",
)


async def _migrate_legacy() -> None:
    """把「一个用户一行 + 目标列表」的旧数据拆成「一个目标一行」（幂等）。"""
    table = WavesStaminaNotice.__tablename__
    async with engine.connect() as conn:
        existing = await conn.run_sync(_table_columns, table)
    if not existing:
        return
    legacy = [c for c in _LEGACY_COLUMNS if c in existing]
    if not legacy or "target_type" not in existing:
        return

    select_cols = ["id", "user_id", "bot_id", "uid"] + [
        c for c in _CARRY_COLUMNS if c in existing
    ] + legacy
    quoted = ", ".join(f'"{c}"' for c in select_cols)
    sql = (
        f'SELECT {quoted} FROM "{table}" '
        f"WHERE COALESCE(target_type, '') NOT IN ('private', 'group')"
    )
    async with engine.connect() as conn:
        result = await conn.execute(text(sql))
        rows = [dict(r._mapping) for r in result.fetchall()]
    if not rows:
        return

    logger.info(f"[鸣潮体力提醒] 发现 {len(rows)} 行旧数据，开始拆分推送目标")

    for row in rows:
        user_id = row.get("user_id") or ""
        bot_id = row.get("bot_id") or ""
        uid = row.get("uid") or ""
        carry = {c: row.get(c) for c in _CARRY_COLUMNS if c in row}

        targets: List[tuple] = []
        private_flag = row.get("push_private") or 0
        legacy_target = row.get("push_target") or ""
        if private_flag or legacy_target == "private":
            targets.append((TARGET_PRIVATE, ""))
        raw_groups = row.get("group_ids") or ""
        if not raw_groups and legacy_target == "group":
            raw_groups = row.get("group_id") or ""
        for group_id in [g.strip() for g in str(raw_groups).split(",")]:
            if group_id:
                targets.append((TARGET_GROUP, group_id))

        for target_type, target_id in targets:
            try:
                await WavesStaminaNotice.upsert_row(
                    user_id,
                    bot_id,
                    uid,
                    target_type,
                    target_id,
                    **carry,
                )
            except Exception as e:
                logger.warning(
                    f"[鸣潮体力提醒] 迁移 {uid} -> {target_type}:{target_id} 失败: {e}"
                )

        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(f'DELETE FROM "{table}" WHERE id = :rid'), {"rid": row["id"]}
                )
        except Exception as e:
            logger.warning(f"[鸣潮体力提醒] 删除旧行 {row['id']} 失败: {e}")


async def _dedupe_and_index() -> None:
    """去重后建唯一索引，保证同一目标只有一行。"""
    table = WavesStaminaNotice.__tablename__
    keys = 'user_id, bot_id, uid, target_type, COALESCE(target_id, \'\')'
    dedupe_sql = (
        f'DELETE FROM "{table}" WHERE id NOT IN ('
        f"SELECT mid FROM (SELECT MIN(id) AS mid FROM \"{table}\" GROUP BY {keys}) AS x)"
    )
    index_sql = (
        f'CREATE UNIQUE INDEX IF NOT EXISTS "ix_{table}_target" '
        f'ON "{table}" (user_id, bot_id, uid, target_type, target_id)'
    )
    for sql in (dedupe_sql, index_sql):
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception as e:
            logger.debug(f"[鸣潮体力提醒] 索引步骤跳过: {e}")


async def _log_stats() -> None:
    """启动时打一行统计，方便确认数据到底存在哪、有几条。"""
    table = WavesStaminaNotice.__tablename__
    sql = (
        f'SELECT target_type, COUNT(1) FROM "{table}" '
        f"GROUP BY target_type"
    )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            stats = {str(row[0] or "未知"): row[1] for row in result.fetchall()}
    except Exception as e:
        logger.debug(f"[鸣潮体力提醒] 统计行数失败: {e}")
        return
    if not stats:
        logger.info(f"[鸣潮体力提醒] 表 {table} 当前没有任何订阅")
        return
    detail = "、".join(
        f"{'私聊' if k == TARGET_PRIVATE else ('群' if k == TARGET_GROUP else k)} {v} 条"
        for k, v in sorted(stats.items())
    )
    logger.info(f"[鸣潮体力提醒] 表 {table} 现有订阅：{detail}")


async def ensure_tables() -> None:
    """建表 + 补列 + 旧数据拆分 + 唯一索引（全部幂等）。"""
    table = WavesStaminaNotice.__table__  # type: ignore[attr-defined]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=[table]))
    await _add_missing_columns(table)
    await _migrate_legacy()
    await _dedupe_and_index()
    await _log_stats()


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
    logger.debug("[鸣潮体力提醒] 未注册后台管理页")
