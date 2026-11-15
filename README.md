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
[鸣潮体力提醒] 数据表 WavesStaminaNotice 就绪
[鸣潮体力提醒] 已接入 XutheringWavesUID（检查间隔 6 分钟，催命间隔 60 分钟）
```

## 指令

所有指令都**只作用于当前会话**：私聊里发就改私聊那条记录，群里发就改本群那条记录。

| 指令 | 说明 |
|---|---|
| `ww开启体力提醒` | 在当前会话开启提醒（群里需要群管理）；私聊一条、每个群各一条，互不影响 |
| `ww关闭体力提醒` | 只关闭当前会话的提醒（私聊只关私聊，群里只关本群） |
| `ww关闭全部提醒` | 一次性清掉私聊 + 所有群 |
| `ww设置提醒阈值240` | 设置**当前会话**的阈值（1 ~ 240，上限可在配置里改） |
| `ww设置提醒阈值` | 查看状态（等价于 `ww体力提醒状态`） |
| `ww开启催命模式` | 当前会话的提醒开启催命，体力满后按 `CrazyMinutes` 反复提醒 |
| `ww关闭催命模式` | 关闭当前会话的催命，普通提醒保留 |

群消息里 `@` 的是订阅者本人（不是 @ 全体）。同一个人可以同时收到私聊和多个群的提醒，
每条目标各自有独立的阈值、催命状态、计时和推送计数。

## 工作方式

```
每 6 分钟一轮
  └─ 查一次 DB：next_check_ts <= now 的行（一行 = 一个推送目标）
       └─ 按 (user_id, bot_id, uid) 聚合，每个账号只调一次体力 API
            ├─ 体力 < 阈值：用 refreshTimeStamp 反推「刚好到阈值」的时刻，把下次检查钉过去
            └─ 体力 ≥ 阈值：逐行判定
                 ├─ 普通模式：靠 last_energy 的 低→高 跃迁，只推一次
                 └─ 催命模式：每 CrazyMinutes 再推一次，直到关闭或体力被清
```

因此没有「每分钟轮询全体用户」的开销：常态下一轮只产生一条 SQL，单个账号每 6~7 小时才
真正打一次 API；同一账号的私聊行和群行共用这次采样。

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

**一行 = 一个推送目标**，唯一键 `(user_id, bot_id, uid, target_type, target_id)`。

| 字段 | 说明 |
|---|---|
| `user_id` / `bot_id` | 平台用户与平台 |
| `uid` | 鸣潮 UID |
| `bot_self_id` | 精确投递用的 bot 自身 ID |
| `target_type` | `private`（私聊）/ `group`（群） |
| `target_id` | 私聊留空；群聊填群号 |
| `threshold` | 该目标的体力阈值 |
| `next_check_ts` | 下次检查时间戳（0 = 立即） |
| `last_energy` | 上次采样体力，用于跃迁判定 |
| `crazy` | 该目标的催命开关 `on` / `off` |
| `last_push_ts` / `push_count` | 上次推送时间 / 累计推送次数（防重复与统计） |
| `fail_count` / `last_error` | 连续取数失败次数 / 上次错误，便于排障 |

举例：同一个人在私聊开一次、在群 A、群 B 各开一次，表里是 3 行：

| target_type | target_id | threshold | crazy |
|---|---|---|---|
| private | （空） | 240 | off |
| group | 123456 | 235 | on |
| group | 789012 | 240 | off |

在群 A 发 `ww设置提醒阈值235` 只改第二行；私聊的阈值仍是 240。

> 旧版「一个用户一行 + `group_ids` 列表」的结构会在启动时自动拆分迁移：
> 私聊开关转成 `private` 行，`group_ids` 里每个群各转成一行，旧行随后删除。

## 排障

- `❌ 未检测到可用的 XutheringWavesUID`：对方未安装/未启用，或 import 失败（看核心日志里的
  `[鸣潮体力提醒] 依赖 XutheringWavesUID 不可用` 那一行的具体异常）。
- `❌ 未绑定 cookie`：先在对方插件里用 `ww每日` 或 `ww登录` 完成一次登录。
- 状态里 `上次取数错误` 有内容：说明该 UID 的 cookie 已失效，重登即可。
