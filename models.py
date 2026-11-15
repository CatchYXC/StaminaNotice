"""StaminaNotice 自建数据表（一行 = 一个推送目标）。

数据模型
--------
唯一键：``(user_id, bot_id, uid, target_type, target_id)``

"""

import time
from typing import Any, Dict, List, Optional, Type, TypeVar

from sqlalchemy import BigInteger, Boolean, Float, Integer, Numeric
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


def _index_names(sync_conn, name: str) -> List[str]:
    insp = sa_inspect(sync_conn)
    try:
        return [i["name"] for i in insp.get_indexes(name) if i.get("name")]
    except Exception:
        return []


def _fallback_value(column) -> Any:
    """列缺省值，用于旧行补空。"""
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        return default.arg
    if isinstance(column.type, (Integer, BigInteger, Boolean)):
        return 0
    if isinstance(column.type, (Float, Numeric)):
        return 0.0
    return ""


def _split_targets(row: Dict[str, Any]) -> List[tuple]:
    """旧行 -> 若干 (target_type, target_id)。"""
    targets: List[tuple] = []
    private_flag = row.get("push_private") or 0
    legacy_target = str(row.get("push_target") or "").strip()
    if private_flag or legacy_target == "private":
        targets.append((TARGET_PRIVATE, ""))
    raw_groups = row.get("group_ids") or ""
    if not raw_groups and legacy_target == "group":
        raw_groups = row.get("group_id") or ""
    for group_id in str(raw_groups).split(","):
        group_id = group_id.strip()
        if group_id:
            targets.append((TARGET_GROUP, group_id))
    return targets


def _build_row(
    columns, source: Dict[str, Any], target_type: str, target_id: str
) -> Dict[str, Any]:
    """按模型列拼一行新数据。"""
    row: Dict[str, Any] = {}
    for column in columns:
        name = column.name
        if name == "id":
            continue
        if name == "target_type":
            row[name] = target_type
            continue
        if name == "target_id":
            row[name] = target_id or ""
            continue
        value = source.get(name)
        if value is None:
            value = _fallback_value(column)
        row[name] = value
    return row


async def _normalize_table() -> None:
    """把旧表结构彻底换成新表结构：拆目标 + 删除废弃的 NOT NULL 旧列。

    旧表里的 ``push_target`` / ``group_id`` 等列是 ``NOT NULL`` 且没有默认值，
    新模型已经不写这些列，因此只要它们还在表里，任何 INSERT 都会被
    ``NOT NULL constraint failed`` 拒绝。SQLite 不支持修改列约束，
    只能重建表。
    """
    table_name = WavesStaminaNotice.__tablename__
    table = WavesStaminaNotice.__table__  # type: ignore[attr-defined]
    columns = list(table.columns)
    model_cols = [c.name for c in columns]

    async with engine.connect() as conn:
        existing = await conn.run_sync(_table_columns, table_name)
        dialect = conn.dialect.name
    if not existing:
        return

    legacy = [c for c in existing if c not in model_cols]
    if not legacy:
        return

    logger.info(f"[鸣潮体力提醒] 检测到旧列 {legacy}，开始重建表 {table_name}")

    # 1. 旧数据全部读进内存，顺便算出拆分后的行
    quoted = ", ".join(f'"{c}"' for c in existing)
    async with engine.connect() as conn:
        result = await conn.execute(text(f'SELECT {quoted} FROM "{table_name}"'))
        old_rows = [dict(r._mapping) for r in result.fetchall()]

    new_rows: List[Dict[str, Any]] = []
    seen = set()
    skipped = 0
    for row in old_rows:
        target_type = str(row.get("target_type") or "").strip()
        if target_type in TARGET_TYPES:
            candidates = [(target_type, str(row.get("target_id") or ""))]
        else:
            candidates = _split_targets(row)
        if not candidates:
            skipped += 1
            continue
        for t_type, t_id in candidates:
            built = _build_row(columns, row, t_type, t_id)
            key = (
                built.get("user_id") or "",
                built.get("bot_id") or "",
                built.get("uid") or "",
                t_type,
                t_id or "",
            )
            if key in seen:
                continue
            seen.add(key)
            new_rows.append(built)

    # 2. 换表结构
    if dialect == "sqlite":
        tmp = f"{table_name}__legacy"
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{tmp}"'))
            await conn.execute(text(f'ALTER TABLE "{table_name}" RENAME TO "{tmp}"'))
        # 索引名在 SQLite 里是全局的，先摘掉，否则新表建索引会重名
        async with engine.connect() as conn:
            names = await conn.run_sync(_index_names, tmp)
        for name in names:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
            except Exception as e:
                logger.debug(f"[鸣潮体力提醒] 删除旧索引 {name} 跳过: {e}")
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=[table]))
        logger.info("[鸣潮体力提醒] 新表已创建，准备回写数据")
    else:
        for name in legacy:
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(f'ALTER TABLE "{table_name}" DROP COLUMN "{name}"')
                    )
            except Exception as e:
                logger.warning(f"[鸣潮体力提醒] 删除旧列 {name} 失败: {e}")
        # 老版本 MySQL 不支持 DROP COLUMN 时，宁可不动数据也不要写坏
        async with engine.connect() as conn:
            still = await conn.run_sync(_table_columns, table_name)
        left = [c for c in (still or []) if c not in model_cols]
        if left:
            logger.warning(
                f"[鸣潮体力提醒] 旧列 {left} 未能删除，跳过数据迁移以保护现有数据；"
                "请手动重建该表后再启动"
            )
            return

    # 3. 回写
    async with engine.begin() as conn:
        await conn.execute(text(f'DELETE FROM "{table_name}"'))
    await _insert_rows(table_name, columns, new_rows)

    # 4. 丢掉旧表
    if dialect == "sqlite":
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP TABLE IF EXISTS "{tmp}"'))
        except Exception as e:
            logger.warning(f"[鸣潮体力提醒] 删除旧表失败: {e}")

    logger.info(
        f"[鸣潮体力提醒] 重建完成：读入 {len(old_rows)} 行，"
        f"写入 {len(new_rows)} 行，丢弃 {skipped} 行无效数据"
    )


async def _insert_rows(table_name: str, columns, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    names = [c.name for c in columns if c.name != "id"]
    cols_sql = ", ".join('"' + n + '"' for n in names)
    sql = (
        f'INSERT INTO "{table_name}" ({cols_sql}) '
        f'VALUES ({", ".join(":" + n for n in names)})'
    )
    for i in range(0, len(rows), 200):
        chunk = rows[i : i + 200]
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), chunk)
        except Exception as e:
            logger.warning(f"[鸣潮体力提醒] 回写 {len(chunk)} 行失败: {e}")


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
    """建表 + 补列 + 旧表重建（拆目标、去废弃列）+ 唯一索引（全部幂等）。"""
    table = WavesStaminaNotice.__table__  # type: ignore[attr-defined]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=[table]))
    await _add_missing_columns(table)
    await _normalize_table()
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
