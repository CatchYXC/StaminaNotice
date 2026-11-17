"""StaminaNotice 的默认配置项。

全部配置独立于 XutheringWavesUID，落在本插件自己的 config.json。
"""

from typing import Dict

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsBoolConfig,
)

CONFIG_DEFAULT: Dict[str, GSC] = {
    "StaminaNotice": GsBoolConfig(
        "体力提醒总开关",
        "关闭后所有体力提醒与催命都停止（用户侧设置保留，重新打开即恢复）",
        True,
    ),
    "CrazyNotice": GsBoolConfig(
        "催命模式总开关",
        "关闭后即使单个用户开启了催命模式也不会重复提醒",
        True,
    ),
    "CheckMinutes": GsIntConfig(
        "体力检查间隔(分钟)",
        "建议 6，与鸣潮体力每 6 分钟恢复 1 点对齐；修改后需重载插件生效",
        6,
    ),
    "CrazyMinutes": GsIntConfig(
        "催命重复提醒间隔(分钟)",
        "催命模式下体力满之后每隔多少分钟再提醒一次",
        60,
    ),
    "RecheckMinutes": GsIntConfig(
        "体力满后的重新采样间隔(分钟)",
        "普通模式推送过一条后会等待这段时间再采样，用于发现用户已经清完体力并重新计时",
        120,
    ),
    "ThresholdMax": GsIntConfig(
        "体力阈值上限",
        "用户可设置的最大体力阈值，默认 240（鸣潮结晶波片上限）",
        240,
    ),
    "PushImage": GsBoolConfig(
        "推送图片",
        "开启后提醒渲染成与「每日」一致的体力卡片图片推送（复用 XutheringWavesUID 的素材）；"
        "渲染失败会自动退回纯文字，关闭则一直发文字",
        True,
    ),
    "DebugMode": GsBoolConfig(
        "调试模式",
        "打开后才能使用 [ww测试推送]（立刻按正式样式发一条，不写入计时与推送次数）；"
        "同时会在日志里打印每个目标的判定明细。平时建议关闭",
        False,
    ),
}
