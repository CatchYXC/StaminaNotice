"""启动钩子：建表 + 依赖自检。

``on_core_start`` 在全部插件 import 完成后才执行，此时 XutheringWavesUID 一定
已经在 ``sys.modules`` 里，预热不会触发它的模块级重型初始化。
"""

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start

from . import stamina_config as cfg
from . import xw_bridge
from .models import ensure_tables


@on_core_start
async def _stamina_notice_startup() -> None:
    try:
        await ensure_tables()
        logger.info("[鸣潮体力提醒] 数据表 WavesStaminaNotice 就绪")
    except Exception as e:
        logger.exception(f"[鸣潮体力提醒] 建表失败，插件功能不可用: {e}")
        return

    if xw_bridge.ensure():
        logger.info(
            "[鸣潮体力提醒] 已接入 XutheringWavesUID"
            f"（检查间隔 {cfg.check_minutes()} 分钟，催命间隔 {cfg.crazy_minutes()} 分钟）"
        )
        logger.info(f"[鸣潮体力提醒] 依赖模块：{xw_bridge.describe()}")
    else:
        logger.warning(
            "[鸣潮体力提醒] 未检测到 XutheringWavesUID，指令会提示用户安装；"
            f"原因：{xw_bridge.error()}"
        )
