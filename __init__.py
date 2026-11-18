"""StaminaNotice - 鸣潮体力提醒推送（独立插件）。

与 XutheringWavesUID 同时安装即可使用：
- 账号 / 绑定 / 体力数据：只读借用 XutheringWavesUID（见 xw_bridge.py）
- 推送图片：直接调用对方「每日」的绘图入口，拿到图后自己配文
- 提醒配置与推送状态：全部落在本插件自建表 WavesStaminaNotice
- 除出图时对方自行记账（WavesStaminaRecord 的一次查询记录）外，不写对方的表
"""

from gsuid_core.logger import logger
from gsuid_core.sv import SL, Plugins

# 幂等：跨插件 cross-import 时本文件可能在新 namespace 下被再次执行，
# 重复 Plugins() 会用默认值覆盖已有的 disable_force_prefix 等配置。
if "StaminaNotice" not in SL.plugins:
    Plugins(name="StaminaNotice", force_prefix=["ww"], allow_empty_prefix=False)

# 导入顺序有意义：配置 -> 模型 -> 取数桥 -> 引擎(注册定时任务) -> 指令 -> 启动钩子
from . import stamina_config  # noqa: E402,F401
from . import models  # noqa: E402,F401
from . import xw_bridge  # noqa: E402,F401
from . import push_engine  # noqa: E402,F401
from . import commands  # noqa: E402,F401
from . import bootstrap  # noqa: E402,F401

logger.debug("[鸣潮体力提醒] StaminaNotice 插件已加载")
