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

`install.ps1` 注册两个任务（都无需管理员权限，都以 `pythonw.exe` 运行，不弹窗）：

| 任务名 | 触发时间 | 时间上限 | 作用 |
|---|---|---|---|
| `WB-SignIn-Main` | 每天 **09:10** | 10 分钟 | 主签到 |
| `WB-SignIn-Poll` | 每天 **12:00 / 15:00 / 18:00 / 21:00** | 5 分钟 | 兜底补签 |

**为什么要两个任务**：主任务万一撞上关机、睡眠或刚开机网络没就绪，当天就再没机会了、连签直接断。
兜底轮询每次都会先查状态，**已签的话只发一个查询请求就退出**，代价可以忽略，换来的是一天五次补救机会。

两个任务都开启了：

- **错过计划后尽快启动** —— 关机期间错过的任务，下次开机自动补跑
- **唤醒计算机执行** —— 睡眠状态下到点可唤醒（不想用可以加 `-NoWake`）

自定义安装：

```powershell
# 改主任务时间
powershell -ExecutionPolicy Bypass -File .\install.ps1 -MainTime "08:30"

# 改兜底时间
powershell -ExecutionPolicy Bypass -File .\install.ps1 -PollTimes "11:00","14:00","19:00"

# 手动指定解释器（自动探测失败时）
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Pythonw "D:\miniconda\pythonw.exe"
```

> 脚本会自动优先选择**非 WorkBuddy 自带**的 Python——因为自带运行时装在带版本号的目录里，
> WorkBuddy 升级后可能被移走，会导致任务静默失效。可以用环境变量 `WORKBUDDY_PYTHONW` 覆盖。

卸载（不会删除日志和状态文件）：

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

核验：

```powershell
schtasks /query /tn "WB-SignIn-Main" /fo LIST /v
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
state.json            脚本自建：当日签到状态，含连续天数、累计积分、触发来源
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

---

## 协议

本项目基于 [MIT License](LICENSE) 开源，Copyright (c) 2026 yungu-ovo。

需要说明的是：MIT 协议授权的是**本仓库的代码本身**，不等于获得 WorkBuddy 服务或其接口的任何授权，
也不代表本项目与腾讯有任何关联。接口的使用仍需遵守腾讯的相关服务条款，
相关风险请见文首的〈先说风险〉一节。

