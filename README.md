# WorkBuddy 每日自动签到

读取本机 WorkBuddy 桌面端已登录的凭据，每天自动领取「Buddy 加油站」的签到积分。
**纯 Python 标准库，零第三方依赖；不开窗口、不改登录态、不上传任何凭据。**

---

## ⚠️ 先说风险（请务必读完）

1. **本工具非官方**，与腾讯 / WorkBuddy 无任何关联。签到接口是从桌面端逆向得到的私有接口，
   腾讯改一版就可能失效，需要自行跟进维护。
2. **平台服务协议通常禁止用脚本/机器人自动化调用服务**。使用可能触发风控，
   极端情况导致积分被清理或账号受限。**请只用于你自己的个人单账号，不要做多账号批量刷取。**
3. **`accessToken` 等同于登录密码**。本脚本保证：只读本机、不打印、不写入日志、不落任何配置文件、不出本机。
   请勿把登录态文件或其中的 token 提交到 Git、贴到聊天群里。
4. **不要把它搬到云服务器或 GitHub Actions**。异地 IP + 没有本机设备特征，风控风险明显更高。
   本方案是纯本机调度，就是为了规避这一点。
5. 签到活动有固定期限（接口会返回 `end_time`），到期后脚本会自动停止，不会空转报错。

使用即表示你理解并自愿承担上述风险。

---

## 工作原理

WorkBuddy 客户端点「领取积分」这个动作，本质是带登录态发一次 HTTP 请求。这个脚本直接调用同一组接口：

| 用途 | 方法 | 路径 |
|---|---|---|
| 查询签到状态 | POST | `/v2/billing/meter/checkin-activity-status` |
| 领取每日积分 | POST | `/v2/billing/meter/daily-checkin` |

请求头：`Authorization: Bearer <accessToken>` + `X-User-Id: <uid>`。

**执行顺序（天然幂等）**：

1. 读取登录态 → 取 `accessToken` / `uid` / `domain`
2. 查状态 → **今天已签就直接结束，不发签到请求**
3. 未签才调用签到接口
4. 回读校验，确认服务端状态已变为「今日已签」

即使一天跑十次，也只会领到一次。签到接口本身也是幂等的：重复调用只会返回「今天已签到」。

**几个踩过的坑（已在本脚本中处理）**：

- **域名不能写死**。网上教程流传 `copilot.tencent.com`、`codebuddy.cn`，实测都会 404。
  正确做法是读登录态文件里的 `auth.domain`——客户端连哪个就用哪个。本机实测值是 `www.workbuddy.cn`。
- **不要模拟鼠标点击**。客户端是 Electron，`isTrusted` 过滤会让模拟点击失效，所以走接口是唯一稳的路。
- **登录态文件会被客户端短暂独占**，直接读会 `Permission denied`。脚本内置了 4 次短间隔重试。
  这类瞬时占用**不会**被当成「登录失效」，也不会触发告警。

---

## 快速开始

把本仓库的文件放到一个**固定目录**（不要放临时目录，计划任务会长期引用它），
后续命令都在该目录下执行。以下示例假定目录是 `D:\tools\workbuddy-checkin\`。

### 第 1 步：离线自检（不联网）

```powershell
python wb_signin.py doctor
```

会检查登录态文件是否找得到、字段是否齐全、token 是否已过期。全程不联网、不打印任何凭据。

### 第 2 步：只读查询（不改任何东西）

```powershell
python wb_signin.py status
```

输出类似：

```
今日尚未签到（连续 1 天，累计 100 积分，每日 100 积分）
活动：Buddy应用
活动周期：2026-09-30 00:00:00 → 2026-10-15 23:59:59
```

### 第 3 步：先演练一次

```powershell
python wb_signin.py auto --dry-run
```

只跑链路、**绝不调用签到接口**、不写状态文件。

### 第 4 步：装定时任务

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

装完终端会打印两个任务和下次运行时间。**不需要管理员权限。**

---

## 命令参考

```powershell
python wb_signin.py doctor    # 离线自检：凭据文件、字段、过期时间。不联网
python wb_signin.py status    # 只读查询今日签到状态
python wb_signin.py claim     # 只签到（跳过状态预检）
python wb_signin.py auto      # 默认：先查状态 → 已签跳过 → 未签才签 → 回读校验
```

通用参数：

| 参数 | 作用 |
|---|---|
| `--dry-run` | 演练模式：绝不调用签到接口，不写 state.json |
| `--no-notify` | 本次不推送任何通知 |
| `--fields` | 打印接口响应的字段结构（**只有字段名，没有值**），用于接口变更后校准 |
| `--json` | 结果以单行 JSON 输出，便于被其它程序消费 |
| `--quiet` | 不输出到控制台（定时任务用） |
| `--source NAME` | 标记来源（`main` / `poll`），写入 state.json 便于事后区分触发者 |

退出码：

| 码 | 含义 |
|---|---|
| `0` | 成功，或今日已签，或活动已结束 |
| `1` | 网络 / 服务端错误（已重试仍失败），或登录态文件被瞬时占用 |
| `2` | 部分完成（时间预算耗尽），留给下一轮兜底 |
| `3` | 凭据失效或格式异常，**需要人工重新登录客户端** |

---

## 定时任务

`install.ps1` 只注册**一个**任务 `WB-SignIn`（无需管理员权限，以 `pythonw.exe` 运行，不弹窗），
但给它挂了三个触发器：

| 触发器 | 时机 | 作用 |
|---|---|---|
| 心跳 | **每 30 分钟一次**，无限重复 | 机器只要醒着，最多 30 分钟就会跑一次 |
| 登录 | 登录后 30 秒 | 开机 / 重新登录后立刻补上 |
| 解锁 | 锁屏解锁后 | 合盖睡眠唤醒、解锁后立刻补上 |

### 为什么不用固定时点（这是踩过的坑）

早期版本用的是固定时刻：主任务 **09:10** + 兜底 **12:00 / 15:00 / 18:00 / 21:00**。
看着很合理，实际在笔记本上完全靠不住 —— 2026-10-05 那天一分都没领到：

- 09:10 和 12:00 两个时点机器都在睡眠（当天 **12:03** 才唤醒），双双落空；
- 任务的「唤醒计算机执行」形同虚设：本机电源计划的「允许使用唤醒定时器」是
  **交流 = 仅限重要的唤醒定时器 / 电池 = 禁用**，而第三方计划任务不算「重要」，唤不醒；
- Windows 的「错过计划后尽快启动」也并不可靠 —— 同样是错过的 09:10，
  10-04 补跑了（延迟 1 小时 41 分），10-03 和 10-05 就没补。

固定时点的根本问题在于，**它假设机器在某个时刻一定是醒着的**，而这个假设不成立。
换成「心跳 + 登录 + 解锁」之后，签到只依赖「机器醒着」这一个条件 —— 而这一条必然成立。

### 为什么可以这么频繁

脚本天然幂等：每次都先查状态，已签就直接退出。所以绝大多数心跳只花 **1 次 HTTPS 请求**
（约 1 KB），一天约 28 次。同机同 IP、低频，不构成风控风险；签到接口本身也幂等，
重复调用只会返回「今天已签到」，不可能重复领取。

> 活动 2026-10-15 结束后，可以把间隔调大（`-IntervalMinutes 60`）或者直接卸载。

任务还开启了：

- **错过计划后尽快启动** —— 关机 / 睡眠期间错过的，恢复后补跑
- **电池上照常运行** —— 不因为拔了电源就罢工
- **多实例忽略** —— 永远只有一个实例在跑，不会自己撞自己

自定义安装：

```powershell
# 改心跳间隔（默认 30 分钟，不建议低于 15）
powershell -ExecutionPolicy Bypass -File .\install.ps1 -IntervalMinutes 60

# 改心跳的起始时点
powershell -ExecutionPolicy Bypass -File .\install.ps1 -StartAt "09:00"

# 手动指定解释器（自动探测失败时）
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Pythonw "D:\miniconda\pythonw.exe"

# 如果你的电源计划允许唤醒定时器，想让任务顺便把机器唤醒
powershell -ExecutionPolicy Bypass -File .\install.ps1 -EnableWake
```

> 脚本会自动优先选择**非 WorkBuddy 自带**的 Python —— 因为自带运行时装在带版本号的目录里，
> WorkBuddy 升级后可能被移走，会导致任务静默失效。可以用环境变量 `WORKBUDDY_PYTHONW` 覆盖。
>
> 安装脚本会读一下电源计划的唤醒定时器设置并给出提示，但**不会**去改它 ——
> 改电源计划需要管理员权限，而这个脚本刻意只需要普通权限。

卸载（不会删除日志和状态文件；也会顺带清理旧版留下的 `WB-SignIn-Main` / `WB-SignIn-Poll`）：

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

核验：

```powershell
# 看三个触发器是否都在
powershell -Command "Get-ScheduledTask -TaskName WB-SignIn | Select -Expand Triggers"
# 上次运行时间、下次运行时间、上次退出码
powershell -Command "Get-ScheduledTaskInfo -TaskName WB-SignIn"
# 立刻手动跑一次
powershell -Command "Start-ScheduledTask -TaskName WB-SignIn"
# 日志与状态
python wb_signin.py doctor
Get-Content .\signin.log -Tail 5
Get-Content .\state.json
```

---

## 失败通知（可选）

默认**不推送任何通知**，只写本地日志。

要开启的话，复制 `config.example.json` 为 `config.json`，填入任一渠道：

```json
{
  "serverchan_sendkey": "SCT开头的SendKey",
  "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
}
```

也可以用环境变量 `WORKBUDDY_SERVERCHAN_KEY` / `WORKBUDDY_WECOM_WEBHOOK` 覆盖。

**只在失败时推送**（凭据失效、接口持续失败）。签到成功一律不打扰——每天一条「签到成功」
很快就会变成背景噪音被人自动忽略，而一旦习惯了忽略，真正重要的失败告警也就跟着被漏掉了。

> `config.json` 里**不要**放 token。凭据始终从本机登录态文件实时读取。

---

## 文件说明

```
wb_signin.py          主脚本（单文件，纯标准库）
install.ps1           注册计划任务
uninstall.ps1         移除计划任务
config.example.json   通知配置模板（复制为 config.json 才生效）
state.json            脚本自建：当日签到状态 + 运行心跳（上次运行、上次成功、已错过天数）
signin.log            运行日志，按天轮转，保留 30 天
```

环境变量：

| 变量 | 作用 |
|---|---|
| `WORKBUDDY_AUTH_FILE` | 手动指定登录态文件路径（自动探测失败时） |
| `WORKBUDDY_PYTHONW` | 指定 `pythonw.exe` 路径 |
| `WORKBUDDY_SIGNIN_LOG` | 覆盖日志文件路径 |
| `WORKBUDDY_BUDGET_SECONDS` | 单次运行的时间预算（默认 420 秒，上限 540） |

---

## 常见问题

**Q：提示「登录态失效，需要人工处理」怎么办？**
打开 WorkBuddy 桌面端确认已登录，然后重跑 `python wb_signin.py status`。
客户端会自动续期 token，续期后脚本就恢复正常。

**Q：日志里出现「登录态文件被占用，X 秒后重试」？**
正常现象。客户端正在写这个文件，脚本会自动重试。只要最终结果不是失败就不用管。

**Q：提示「accessToken 是加密信封（$wbEncrypted）」？**
说明客户端升级后启用了加密凭据存储。本脚本**不解密任何凭据**，会直接报错而不是带着无效令牌发请求。
这时需要改用已适配该格式的工具。

**Q：签到接口报错或字段对不上？**
先跑 `python wb_signin.py status --fields` 看接口现在的字段结构（只打印字段名），
再对照 `wb_signin.py` 里的 `BOOL_KEYS` / `STREAK_KEYS` / `CREDIT_KEYS` 调整。

**Q：会不会重复领取？**
不会。脚本先查状态、已签就跳过；签到接口本身也幂等，重复调用只会返回「今天已签到」。

**Q：积分没到账？**
脚本会回读校验并在日志里写明。如果回读发现状态没变，会打一条 warning，据此排查。

**Q：怎么确认计划任务到底跑了没有？**
跑 `python wb_signin.py doctor`。它会打印状态文件概览：**上次运行时间、上次成功签到日期、
已错过的天数**。

这里有个容易踩的坑：脚本**只在运行过的时候才写日志**，所以「日志里什么都没有」既可能是
顺利跳过（当天已签，静默退出），也可能是任务根本没被触发 —— 光看日志分不出来。
`state.json` 里的心跳字段（`last_run_ts` / `last_success_date`）才是能区分这两者的东西，
所以 `auto` / `claim` 的**每一次**运行都会写它，包括失败和登录态被瞬时占用。

另外脚本会自己盯两件事，命中就记一条 WARNING（若配了通知渠道则同时推送）：

- 距上次运行超过 **26 小时** —— 任务可能长时间没被触发（机器本来就关着则属正常）；
- 已整段漏签（**昨天没签上**）—— 连签已断，需要人工看一下。

局限要说清楚：如果机器整天不开机，脚本就不会运行，也就无从报警。所以真正兜底的是
上面那三个触发器（心跳 / 登录 / 解锁），而不是这两个告警。

---

## 协议

本项目基于 [MIT License](LICENSE) 开源，Copyright (c) 2026 yungu-ovo。

需要说明的是：MIT 协议授权的是**本仓库的代码本身**，不等于获得 WorkBuddy 服务或其接口的任何授权，
也不代表本项目与腾讯有任何关联。接口的使用仍需遵守腾讯的相关服务条款，
相关风险请见文首的〈先说风险〉一节。

