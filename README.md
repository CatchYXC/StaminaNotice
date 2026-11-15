# StaminaNotice

鸣潮（Wuthering Waves）体力提醒推送插件。作为**独立插件**运行，与 `XutheringWavesUID` 同时安装，
只读借用它的账号绑定与体力接口，自身不读写对方的任何数据表。

## 与 XutheringWavesUID 的关系

| 维度 | 做法 |
|---|---|
| 账号 / cookie / 绑定 UID | 只读调用 `WavesUser` / `WavesBind`（经 `xw_bridge.py`） |
| 体力数据 | `waves_api.get_daily_info`（国服）/ `launcher_chain.fetch_launcher_panel`（国际服） |
| 提醒配置与推送状态 | 本插件自建表 `WavesStaminaNotice`，**不写入对方任何表** |
| 配置项 | 本插件独立 `StringConfig("鸣潮体力提醒")`，在网页面板单独一张卡片 |
| 对方缺失时 | 指令直接提示「请先安装/启用 XutheringWavesUID」，不影响核心启动 |

所有对 `XutheringWavesUID` 的依赖集中在 `xw_bridge.py` 一个文件里，且全部为**运行时懒加载**
（对方的 `__init__.py` 在模块级有 `copy_build_files()` 与 `create_task()`，不能在顶层 import）。

## 安装

把整个 `StaminaNotice` 目录放进 `gsuid_core/plugins/` 下，重启核心：

```bash
gsuid_core/plugins/StaminaNotice/
gsuid_core/plugins/XutheringWavesUID.../
```

启动日志出现以下两行即正常：

```
[体力提醒] 数据表 WavesStaminaNotice 就绪
[体力提醒] 已接入 XutheringWavesUID（检查间隔 6 分钟，催命间隔 60 分钟）
```

## 指令

| 指令 | 说明 |
|---|---|
| `ww开启体力提醒` | 私聊里开启 → 推送私聊；群聊里开启 → 推送到该群（需要群管理） |
| `ww关闭体力提醒` | 关闭提醒，同时清掉催命标记 |
| `ww设置提醒阈值240` | 设置阈值（1 ~ 240，上限可在配置里改） |
| `ww设置提醒阈值` | 查看当前状态（等价于 `ww体力提醒状态`） |
| `ww开启催命模式` | 体力满后按 `CrazyMinutes` 反复提醒 |
| `ww关闭催命模式` | 停止催命，普通提醒保留 |

## 工作方式

```
每 6 分钟一轮
  └─ 查一次 DB：next_check_ts <= now 的记录
       └─ 只有这些记录会真的调一次体力 API
            ├─ 体力 < 阈值：用 refreshTimeStamp 反推「刚好到阈值」的时刻，把下次检查钉过去
            └─ 体力 ≥ 阈值：靠 last_energy 的 低→高 跃迁判定，只推一次
                 ├─ 普通模式：推完等 RecheckMinutes 再采样，用于发现用户已清体力
                 └─ 催命模式：每 CrazyMinutes 再推一次，直到关闭或体力被清
```

因此没有「每分钟轮询全体用户」的开销：常态下一轮只产生一条 SQL，单个用户每 6~7 小时才
真正打一次 API。

## 配置项（网页面板 → 鸣潮体力提醒）

| Key | 默认 | 说明 |
|---|---|---|
| `StaminaNotice` | 开 | 总开关，关掉后所有提醒与催命停止 |
| `CrazyNotice` | 开 | 催命模式总开关 |
| `CheckMinutes` | 6 | 检查间隔，与体力每 6 分钟恢复 1 点对齐（改后需重载） |
| `CrazyMinutes` | 60 | 催命重复提醒间隔 |
| `RecheckMinutes` | 120 | 体力满后的重新采样间隔 |
| `ThresholdMax` | 240 | 用户可设置的最大阈值 |

## 数据表 `WavesStaminaNotice`

| 字段 | 说明 |
|---|---|
| `user_id` / `bot_id` / `uid` | 订阅维度（联合定位） |
| `bot_self_id` | 精确投递用的 bot 自身 ID |
| `push_target` | `off` / `private` / `group` |
| `group_id` | 群推送目标 |
| `threshold` | 体力阈值 |
| `next_check_ts` | 下次检查时间戳（0 = 立即） |
| `last_energy` | 上次采样体力，用于跃迁判定 |
| `crazy` | 催命开关 `on` / `off` |
| `last_push_ts` / `push_count` | 上次推送时间 / 累计推送次数（防重复与统计） |
| `last_error` | 上次取数错误，便于排障 |

## 排障

- `❌ 未检测到可用的 XutheringWavesUID`：对方未安装/未启用，或 import 失败（看核心日志里的
  `[体力提醒] 依赖 XutheringWavesUID 不可用` 那一行的具体异常）。
- `❌ 未绑定 cookie`：先在对方插件里用 `ww每日` 或 `ww登录` 完成一次登录。
- 状态里 `上次取数错误` 有内容：说明该 UID 的 cookie 已失效，重登即可。
