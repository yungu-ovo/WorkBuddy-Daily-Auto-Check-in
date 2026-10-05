#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日自动签到脚本（单文件 · 纯标准库 · 零依赖）

做什么
    读取本机 WorkBuddy 桌面端已登录的凭据（只读），查询当日签到状态，
    未签到才领取每日积分。接口天然幂等，重复运行不会多领。

不做什么
    · 不点 GUI 按钮（Electron 的 isTrusted 过滤使模拟点击无效）
    · 不修改登录态文件、不切换账号、不上传任何凭据
    · 不做多账号批量（平台风控风险高）
    · 不把 accessToken 打印、写入日志或落盘（accessToken 等同登录密码）

可观测性
    auto / claim 的**每一次**运行（含失败、含登录态文件被瞬时占用）都会写 state.json，
    留下 last_run_ts / last_result / last_success_date / consecutive_miss_days。
    原因是脚本只在「运行过」的时候才写日志 —— 「任务根本没被触发」在日志里全无痕迹，
    只有靠这些字段才能在**下一次**运行时把那次沉默暴露出来。
    另外：距上次运行超过 HEARTBEAT_GAP_HOURS 小时、或整段漏签达到 MISS_ALERT_DAYS 天，
    会记一条 WARNING；配了通知渠道时同时推送。

子命令
    doctor   离线自检：凭据文件是否存在、字段是否齐全、是否已过期。不联网。
    status   只读查询：今天签了没、连续天数、累计积分、活动起止时间。
    claim    只执行签到（不做状态预检）。
    auto     默认：先查状态 → 已签则跳过 → 未签才签到 → 回读校验。

通用参数
    --dry-run      只演练不写操作：绝不调用签到接口，不修改 state.json
    --no-notify    本次运行不推送任何通知
    --fields       额外打印接口响应的字段结构（仅字段名，无任何值），用于校准
    --json         结果以单行 JSON 输出（便于被调度器记录）
    --quiet        不输出到控制台（定时任务静默运行时用）

退出码
    0  成功，或今日已签，或活动已结束
    1  网络 / 服务端错误（已重试仍失败）
    2  部分完成（时间预算耗尽等）
    3  凭据失效或格式异常，需要人工重新登录客户端
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

APP_NAME = "WorkBuddySignIn"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2
EXIT_AUTH = 3

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "state.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"
LOG_FILE = Path(os.environ.get("WORKBUDDY_SIGNIN_LOG") or (SCRIPT_DIR / "signin.log"))

# 接口路径。域名不写死：必须从登录态文件的 auth.domain 读取，
# 教程里流传的 copilot.tencent.com 已被证实会 404。
API_STATUS = "/v2/billing/meter/checkin-activity-status"
API_CLAIM = "/v2/billing/meter/daily-checkin"
# 兜底域名。实机校准结果：客户端写入的 auth.domain 是 www.workbuddy.cn，
# 教程里流传的 copilot.tencent.com / codebuddy.cn 均不可靠。
FALLBACK_DOMAIN = "www.workbuddy.cn"

HTTP_TIMEOUT = 20                       # 单次请求超时（秒）
BACKOFF_SCHEDULE = (1, 3, 8)            # 可重试错误的退避间隔
DEFAULT_BUDGET = 420                    # 单次运行的时间预算（秒）

# 可观测性阈值。脚本只在「运行过」的时候才写日志，所以「任务根本没被触发」
# 在日志里是全无痕迹的 —— 下面两个阈值让这种沉默在下一次运行时暴露出来。
MISS_ALERT_DAYS = 1                     # 整段漏掉这么多天（=昨天没签上）就告警
HEARTBEAT_GAP_HOURS = 26                # 距上次运行超过这么多小时就怀疑任务没触发

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 服务端表示「今天已签到」的业务码。市面实测出现过 10001 与 1006 两种，
# 另外再用关键词兜底，避免腾讯改码后误判为失败。
ALREADY_CODES = {10001, 1006}
ALREADY_KEYWORDS = ("已签到", "已经签到", "请明天再来", "already")
AUTH_KEYWORDS = ("未登录", "登录已过期", "登录失效", "token 失效", "invalid token", "unauthorized")
ACTIVITY_END_KEYWORDS = ("活动已结束", "活动结束", "活动已下线", "不在活动", "活动未开始")


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class AuthError(Exception):
    """凭据相关的本地错误：文件缺失、格式非法、字段缺失、已过期。

    transient=True 表示这是瞬时状态（例如登录态文件被客户端独占导致读取失败），
    不应当作「登录失效」处理，也不该触发「请重新登录」这类误导性告警。
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class ApiError(Exception):
    """接口调用错误。kind 决定是否可重试：network / server 可重试，其余不可。"""

    def __init__(self, kind: str, message: str, *, http_status: int | None = None,
                 code: Any = None, retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.code = code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in ("network", "server")


# --------------------------------------------------------------------------
# 日志（带凭据脱敏兜底）
# --------------------------------------------------------------------------


class SecretScrubber(logging.Filter):
    """兜底防线：万一某处把 token 拼进了日志消息，这里直接替换掉。"""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def register(self, secret: str | None) -> None:
        if secret and len(secret) >= 16 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        scrubbed = msg
        for s in self._secrets:
            if s in scrubbed:
                scrubbed = scrubbed.replace(s, "<redacted>")
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


SCRUBBER = SecretScrubber()
LOGGER = logging.getLogger(APP_NAME)

# 本次运行的触发来源，写入 state.json 便于事后区分主任务 / 兜底轮询。
TRIGGER = os.environ.get("WORKBUDDY_TRIGGER", "manual")

# 控制台输出开关。--quiet / --json 时关闭，避免污染机器可读输出。
CONSOLE = True


def say(message: str = "") -> None:
    """只在允许控制台输出时打印（--json / --quiet 下自动静默）。"""
    if CONSOLE and sys.stdout is not None:
        print(message)


def setup_logging(*, quiet: bool) -> None:
    global CONSOLE
    CONSOLE = (not quiet) and sys.stdout is not None

    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    if LOGGER.handlers:
        return
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            LOG_FILE, when="midnight", backupCount=30, encoding="utf-8", delay=True
        )
        fh.setFormatter(fmt)
        fh.addFilter(SCRUBBER)
        LOGGER.addHandler(fh)
    except OSError as exc:  # 日志都写不了就别拦着主流程
        print(f"警告：无法创建日志文件 {LOG_FILE}：{exc}", file=sys.stderr)

    if CONSOLE:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        ch.addFilter(SCRUBBER)
        LOGGER.addHandler(ch)


# --------------------------------------------------------------------------
# 凭据读取
# --------------------------------------------------------------------------


def auth_file_candidates() -> Iterable[Path]:
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        yield Path(os.path.expandvars(override))
        return

    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"

    home = Path.home()
    yield home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
    yield home / ".config" / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"


LOCK_RETRY_DELAYS = (0.0, 1.0, 2.0, 3.0)   # 读登录态文件的重试间隔
_LOCK_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM, getattr(errno, "EDEADLK", -1)}


def _read_auth_text(path: Path) -> str:
    """读取登录态文件。

    WorkBuddy 客户端写入该文件时会短暂独占，导致我们读到 Permission denied。
    实测这是**瞬时**状态（间隔一两秒再读就正常），所以这里做几次短间隔重试，
    并把最终失败标记为 transient —— 绝不当成「登录失效」。
    """
    last_exc: BaseException | None = None

    for attempt, delay in enumerate(LOCK_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise AuthError(
                "登录态文件不存在，可能 WorkBuddy 已退出登录或尚未登录。"
            ) from exc
        except PermissionError as exc:
            last_exc = exc
            if attempt < len(LOCK_RETRY_DELAYS):
                LOGGER.info("登录态文件被占用，%s 秒后重试（第 %d 次）", LOCK_RETRY_DELAYS[attempt], attempt)
        except OSError as exc:
            last_exc = exc
            if getattr(exc, "errno", None) in _LOCK_ERRNOS and attempt < len(LOCK_RETRY_DELAYS):
                LOGGER.info("读取登录态文件暂时失败，稍后重试（第 %d 次）：%s", attempt, exc)
                continue
            raise AuthError(f"读取登录态文件失败：{exc}") from exc

    raise AuthError(
        f"登录态文件被 WorkBuddy 客户端占用，重试 {len(LOCK_RETRY_DELAYS)} 次仍无法读取。"
        "这是瞬时状态，下一轮定时任务会自动重试，无需重新登录。",
        transient=True,
    ) from last_exc


def _normalize_epoch(value: Any) -> float | None:
    """把 seconds / milliseconds 两种时间戳统一成秒。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return value / 1000.0 if value > 1e11 else float(value)


def _fmt_epoch(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "未知"


@dataclass
class Credentials:
    access_token: str
    uid: str
    base_url: str
    domain: str
    expires_at: float | None
    refresh_expires_at: float | None
    source_path: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    @property
    def seconds_left(self) -> float | None:
        return None if self.expires_at is None else self.expires_at - time.time()


def load_credentials() -> Credentials:
    path: Path | None = None
    for candidate in auth_file_candidates():
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        raise AuthError(
            "找不到登录态文件。请先打开 WorkBuddy 桌面端并确认已登录；"
            "若安装在非默认位置，可用环境变量 WORKBUDDY_AUTH_FILE 指定文件路径。"
        )

    try:
        raw = _read_auth_text(path)
    except AuthError:
        raise

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthError(
            "登录态文件不是合法 JSON。可能是客户端换了存储格式，"
            "或文件正好在写入中被读到——稍后重试一次。"
        ) from exc

    if not isinstance(data, dict):
        raise AuthError("登录态文件结构异常（顶层不是对象）。")

    warnings: list[str] = []
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}

    token = auth.get("accessToken") or auth.get("token") or auth.get("access_token")
    if isinstance(token, dict):
        raise AuthError(
            "登录态里的 accessToken 是结构化字段而非明文，说明客户端已启用加密凭据存储。"
            "本脚本无法解密，请改用已适配该格式的工具。"
        )
    if isinstance(token, str) and token.lstrip().startswith("$"):
        raise AuthError(
            "登录态里的 accessToken 是加密信封（形如 $wbEncrypted），说明客户端已启用加密凭据存储。"
            "本脚本不解密任何凭据，绝不带着无效令牌发请求。"
        )
    if not isinstance(token, str) or not token.strip():
        raise AuthError("登录态文件里读不到 auth.accessToken 字段，客户端字段名可能已变更。")
    token = token.strip()

    uid = account.get("uid") or account.get("userId") or account.get("uin")
    if not isinstance(uid, (str, int)) or not str(uid).strip():
        raise AuthError("登录态文件里读不到 account.uid 字段，客户端字段名可能已变更。")
    uid = str(uid).strip()

    domain_raw = auth.get("domain")
    if isinstance(domain_raw, str) and domain_raw.strip():
        domain = domain_raw.strip().strip("/")
    else:
        domain = FALLBACK_DOMAIN
        warnings.append(f"登录态文件里没有 auth.domain，回退到 {FALLBACK_DOMAIN}（不一定正确）")

    base_url = domain if domain.startswith("http") else "https://" + domain
    base_url = base_url.rstrip("/")

    expires_at = _normalize_epoch(auth.get("expiresAt"))
    refresh_expires_at = _normalize_epoch(auth.get("refreshExpiresAt"))
    if expires_at is None:
        warnings.append("登录态文件里没有可解析的 auth.expiresAt，跳过本地过期预检")

    return Credentials(
        access_token=token,
        uid=uid,
        base_url=base_url,
        domain=domain,
        expires_at=expires_at,
        refresh_expires_at=refresh_expires_at,
        source_path=path,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# HTTP 调用
# --------------------------------------------------------------------------


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def api_call(creds: Credentials, path: str, *, deadline: float) -> dict[str, Any]:
    """调用一个 POST 接口，返回解析后的 JSON。失败抛 ApiError。"""
    budget = _remaining(deadline)
    if budget <= 0:
        raise ApiError("budget", "时间预算已耗尽，未发起请求")

    url = creds.base_url + path
    req = urllib.request.Request(url, data=b"{}", method="POST")
    # 请求头尽量与客户端保持一致，降低被风控的概率
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    req.add_header("Authorization", "Bearer " + creds.access_token)
    req.add_header("X-User-Id", creds.uid)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Origin", creds.base_url)
    req.add_header("Referer", creds.base_url + "/")

    status: int
    raw: str
    headers: Any
    timeout = max(1.0, min(HTTP_TIMEOUT, budget))

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            headers = resp.headers
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = exc.headers
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
    except urllib.error.URLError as exc:
        raise ApiError("network", f"网络不可达或超时：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ApiError("network", "请求超时") from exc
    except OSError as exc:
        raise ApiError("network", f"网络异常：{exc}") from exc

    retry_after = None
    try:
        ra = headers.get("Retry-After") if headers else None
        if ra and str(ra).strip().isdigit():
            retry_after = int(str(ra).strip())
    except Exception:
        retry_after = None

    payload: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            payload = parsed
    except json.JSONDecodeError:
        payload = None

    # 200 但不是 JSON —— 基本可以断定被重定向到了登录页
    if payload is None:
        if status in (401, 403):
            raise ApiError("auth", "服务端拒绝：凭据无效或已过期", http_status=status)
        if 200 <= status < 300:
            raise ApiError("auth", "接口返回的不是 JSON，疑似登录态失效被重定向到登录页", http_status=status)
        if status == 429:
            raise ApiError("server", "请求过于频繁（429）", http_status=status, retry_after=retry_after)
        if status >= 500:
            raise ApiError("server", f"服务端错误（HTTP {status}）", http_status=status)
        raise ApiError("client", f"接口返回异常（HTTP {status}）", http_status=status)

    code = payload.get("code")
    msg = str(payload.get("msg") or payload.get("message") or payload.get("error") or "")

    if status in (401, 403):
        raise ApiError("auth", f"凭据无效或已过期（HTTP {status} {msg}）", http_status=status, code=code)
    if status == 429:
        raise ApiError("server", f"请求过于频繁（429 {msg}）", http_status=status, code=code, retry_after=retry_after)
    if status >= 500:
        raise ApiError("server", f"服务端错误（HTTP {status} {msg}）", http_status=status, code=code)

    if code == 0:
        return payload

    if _is_already(payload):
        raise ApiError("already", msg or "今天已签到", http_status=status, code=code)

    if _is_activity_ended(payload):
        raise ApiError("activity", msg or "签到活动已结束", http_status=status, code=code)

    if any(k.lower() in msg.lower() for k in AUTH_KEYWORDS):
        raise ApiError("auth", f"登录态失效：{msg}", http_status=status, code=code)

    raise ApiError("client", f"接口返回业务错误（code={code} {msg}）", http_status=status, code=code)


def _payload_text(payload: dict[str, Any]) -> str:
    parts = []
    for key in ("msg", "message", "error", "error_msg"):
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


def _is_already(payload: dict[str, Any]) -> bool:
    code = payload.get("code")
    if code in ALREADY_CODES:
        return True
    text = _payload_text(payload).lower()
    return any(kw.lower() in text for kw in ALREADY_KEYWORDS)


def _is_activity_ended(payload: dict[str, Any]) -> bool:
    text = _payload_text(payload)
    return any(kw in text for kw in ACTIVITY_END_KEYWORDS)


# --------------------------------------------------------------------------
# 带退避的重试
# --------------------------------------------------------------------------


def call_with_retry(creds: Credentials, path: str, *, deadline: float, label: str) -> dict[str, Any]:
    attempts = len(BACKOFF_SCHEDULE) + 1
    last: ApiError | None = None

    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                LOGGER.info("%s：第 %d 次尝试", label, attempt)
            return api_call(creds, path, deadline=deadline)
        except ApiError as exc:
            last = exc
            if not exc.retryable:
                raise
            if attempt >= attempts:
                break
            delay = exc.retry_after if exc.retry_after else BACKOFF_SCHEDULE[attempt - 1]
            if _remaining(deadline) <= delay + 2:
                raise ApiError("budget", f"{label}：时间预算不足以再重试") from exc
            LOGGER.warning("%s 失败（%s），%d 秒后重试：%s", label, exc.kind, delay, exc)
            time.sleep(delay)

    assert last is not None
    raise last


# --------------------------------------------------------------------------
# 响应解析
# --------------------------------------------------------------------------


@dataclass
class CheckinStatus:
    already_signed: bool | None   # None = 无法从返回中判定
    active: bool | None = None    # 签到活动是否在进行中
    streak_days: Any = None
    total_credits: Any = None
    daily_credit: Any = None      # 当日基础积分
    today_credit: Any = None      # 今日实得积分
    is_streak_day: bool | None = None
    next_streak_day: Any = None
    streak_bonus_days: Any = None
    streak_bonus_credit: Any = None
    week_checkin_days: Any = None
    activity_name: Any = None
    start_time: Any = None
    end_time: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


# 字段名来自 2026-10-01 在本机的实机校准（--fields 输出），
# 后面列的是历史版本可能用过的别名，作为向前兼容的兜底。
BOOL_KEYS = (
    "today_checked_in", "todayCheckedIn", "today_checkin", "checked_in", "checkedIn",
    "is_checked_in", "isCheckedIn", "has_checked_in", "hasCheckedIn", "signed", "is_signed",
    "isSigned", "checkin_status", "checkInStatus",
)
STREAK_KEYS = ("streak_days", "streakDays", "continuous_days", "continuousDays", "days", "streak")
CREDIT_KEYS = ("total_credits", "totalCredits", "credits", "total", "total_score", "accumulate_credits")


def _first(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no", ""):
            return False
        if "un" in text:
            return False
        if "checked" in text or "signed" in text or "done" in text:
            return True
    return None


def parse_status(payload: dict[str, Any]) -> CheckinStatus:
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload

    already = _as_bool(_first(data, BOOL_KEYS))
    if already is None:
        # 有些版本不给布尔字段，只给「连续天数」——大于 0 且今日有记录
        pass

    return CheckinStatus(
        already_signed=already,
        active=_as_bool(_first(data, ("active", "is_active", "isActive"))),
        streak_days=_first(data, STREAK_KEYS),
        total_credits=_first(data, CREDIT_KEYS),
        daily_credit=_first(data, ("daily_credit", "dailyCredit")),
        today_credit=_first(data, ("today_credit", "todayCredit")),
        is_streak_day=_as_bool(_first(data, ("is_streak_day", "isStreakDay"))),
        next_streak_day=_first(data, ("next_streak_day", "nextStreakDay")),
        streak_bonus_days=_first(data, ("streak_bonus_days", "streakBonusDays")),
        streak_bonus_credit=_first(data, ("streak_bonus_credit", "streakBonusCredit")),
        week_checkin_days=_first(data, ("week_checkin_days", "weekCheckinDays")),
        activity_name=_first(data, ("activity_name", "activityName")),
        start_time=_first(data, ("start_time", "startTime")),
        end_time=_first(data, ("end_time", "endTime")),
        raw=payload,
    )


def field_tree(node: Any, depth: int = 0, max_depth: int = 3) -> list[str]:
    """只打印字段名与类型，绝不打印任何值——用于校准接口结构。"""
    lines: list[str] = []
    if depth > max_depth:
        return lines
    pad = "  " * depth
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}- {key}: {type(value).__name__}")
                lines.extend(field_tree(value, depth + 1, max_depth))
            else:
                lines.append(f"{pad}- {key}: {type(value).__name__}")
    elif isinstance(node, list) and node:
        lines.append(f"{pad}- [0]: {type(node[0]).__name__}")
        lines.extend(field_tree(node[0], depth + 1, max_depth))
    return lines


# --------------------------------------------------------------------------
# 状态文件
# --------------------------------------------------------------------------


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(**values: Any) -> None:
    state = read_state()
    state.update(values)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        LOGGER.warning("写入状态文件失败：%s", exc)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _missed_days(last_success_date: Any) -> int | None:
    """从上次成功签到算起，「已经整段错过的天数」。

    昨天成功 → 0；前天成功（昨天漏了）→ 1。无法解析时返回 None。
    """
    if not isinstance(last_success_date, str):
        return None
    try:
        prev = datetime.strptime(last_success_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return max(0, (datetime.now().date() - prev).days - 1)


# --------------------------------------------------------------------------
# 通知（仅失败时）
# --------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def notify_failure(title: str, body: str) -> None:
    """只有失败才推送；未配置渠道时静默跳过。"""
    config = load_config()
    sent = 0

    sendkey = os.environ.get("WORKBUDDY_SERVERCHAN_KEY") or config.get("serverchan_sendkey")
    if isinstance(sendkey, str) and sendkey.strip():
        url = f"https://sctapi.ftqq.com/{sendkey.strip()}.send"
        if _post_form(url, {"title": title, "desp": body}):
            sent += 1

    webhook = os.environ.get("WORKBUDDY_WECOM_WEBHOOK") or config.get("wecom_webhook")
    if isinstance(webhook, str) and webhook.strip():
        payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
        if _post_json(webhook.strip(), payload):
            sent += 1

    if sent:
        LOGGER.info("失败通知已推送（%d 个渠道）", sent)
    else:
        LOGGER.warning("未配置通知渠道，本次仅写本地日志")


def _post_form(url: str, data: dict[str, Any]) -> bool:
    import urllib.parse

    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        LOGGER.warning("通知推送失败：%s", exc)
        return False


def _post_json(url: str, payload: dict[str, Any]) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        LOGGER.warning("通知推送失败：%s", exc)
        return False


# --------------------------------------------------------------------------
# 结果汇总
# --------------------------------------------------------------------------


@dataclass
class Result:
    status: str            # claimed / already / dry-run / activity-ended / auth-error / failed
    message: str
    exit_code: int
    detail: dict[str, Any] = field(default_factory=dict)
    notify: bool = False   # 是否属于「应当推送」的失败

    def as_json(self) -> str:
        payload = {"result": self.status, "report": self.message}
        payload.update(self.detail)
        return json.dumps(payload, ensure_ascii=False)


def human_report(status: CheckinStatus, *, prefix: str = "") -> str:
    bits = []
    if status.streak_days is not None:
        bits.append(f"连续 {status.streak_days} 天")
    if status.total_credits is not None:
        bits.append(f"累计 {status.total_credits} 积分")
    if status.is_streak_day and status.streak_bonus_credit:
        bits.append(f"本期连签奖励 {status.streak_bonus_credit} 积分")
    elif status.daily_credit:
        bits.append(f"每日 {status.daily_credit} 积分")
    tail = ("（" + "，".join(str(b) for b in bits) + "）") if bits else ""
    return prefix + tail


def status_detail(status: CheckinStatus | None) -> dict[str, Any]:
    if status is None:
        return {}
    return {
        "active": status.active,
        "streak_days": status.streak_days,
        "total_credits": status.total_credits,
        "today_credit": status.today_credit,
        "daily_credit": status.daily_credit,
        "week_checkin_days": status.week_checkin_days,
        "activity": status.activity_name,
        "end_time": status.end_time,
    }


def _auth_failure(exc: AuthError, *, prefix: str = "") -> Result:
    """把凭据类错误分成「真失效」与「瞬时占用」两种，避免误告警。"""
    message = f"{prefix}{exc}"
    if exc.transient:
        LOGGER.warning(message)
        # 文件被客户端短暂占用不是账号问题，不推送，免得造成「登录失效」的误判噪音
        return Result("transient", message, EXIT_ERROR)
    LOGGER.error(message)
    return Result("auth-error", message, EXIT_AUTH, notify=True)


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------


def report_state_overview() -> None:
    """离线打印状态文件概览 —— 用来一眼确认「任务到底跑没跑」。"""
    state = read_state()
    say("")
    say("状态文件概览：")
    if not state:
        say("  （没有记录，说明还没有成功运行过）")
        return

    last_run = state.get("last_run_ts") or state.get("ts")
    source = state.get("last_source") or state.get("source") or "未知"
    outcome = state.get("last_result") or state.get("status") or "未知"
    say(f"  上次运行     : {last_run or '未知'}（来源 {source}，结果 {outcome}）")

    last_run_dt = _parse_ts(last_run)
    if last_run_dt is not None:
        hours = (datetime.now() - last_run_dt).total_seconds() / 3600.0
        flag = f"  [超过 {HEARTBEAT_GAP_HOURS} 小时，任务可能没被触发]" if hours > HEARTBEAT_GAP_HOURS else ""
        say(f"  距今         : {hours:.1f} 小时{flag}")

    success = state.get("last_success_date")
    say(f"  上次成功签到 : {success or '未知'}")
    missed = _missed_days(success)
    if missed is not None:
        say(f"  已错过天数   : {missed}（达到 {MISS_ALERT_DAYS} 天会告警）")

    signed_today = bool(state.get("signed")) and state.get("date") == today()
    say(f"  今天         : {'已签到' if signed_today else '未签到'}")


def cmd_doctor(args: argparse.Namespace) -> Result:
    say(f"运行时：Python {sys.version.split()[0]} ({sys.executable})")
    say(f"脚本目录：{SCRIPT_DIR}")
    say(f"日志文件：{LOG_FILE}")
    say(f"状态文件：{STATE_FILE}")
    say(f"配置文件：{CONFIG_FILE} ({'存在' if CONFIG_FILE.exists() else '不存在，通知功能跳过'})")
    report_state_overview()

    say("\n登录态文件候选路径：")
    for candidate in auth_file_candidates():
        mark = "找到" if candidate.is_file() else "     "
        say(f"  [{mark}] {candidate}")

    try:
        creds = load_credentials()
    except AuthError as exc:
        say(f"\n自检失败：{exc}")
        return _auth_failure(exc)

    SCRUBBER.register(creds.access_token)
    say(f"\n已读取登录态：{creds.source_path}")
    say(f"  域名 auth.domain        : {creds.domain}")
    say(f"  accessToken             : 已加载（长度 {len(creds.access_token)}，内容已隐藏）")
    say(f"  account.uid             : 已加载（长度 {len(creds.uid)}，内容已隐藏）")
    say(f"  accessToken 到期时间    : {_fmt_epoch(creds.expires_at)}")
    say(f"  refreshToken 到期时间   : {_fmt_epoch(creds.refresh_expires_at)}")

    if creds.seconds_left is not None:
        left = creds.seconds_left
        if left > 0:
            say(f"  剩余有效时间            : {timedelta(seconds=int(left))}")
        else:
            say("  剩余有效时间            : 已过期")

    for warning in creds.warnings:
        say(f"  [警告] {warning}")

    problems = []
    if creds.is_expired:
        problems.append("accessToken 已过期，请打开 WorkBuddy 桌面端重新登录后再试")
    if creds.refresh_expires_at is not None and creds.refresh_expires_at <= time.time():
        problems.append("refreshToken 也已过期，客户端必然需要重新登录")

    if problems:
        message = "；".join(problems)
        say(f"\n自检未通过：{message}")
        return Result("auth-error", message, EXIT_AUTH, notify=True)

    say("\n自检通过（离线检查，未联网）。")
    say("下一步建议：python wb_signin.py status        # 只读查询今日签到状态")
    return Result("ok", "离线自检通过", EXIT_OK)


def _fetch_status(creds: Credentials, deadline: float) -> tuple[CheckinStatus, str]:
    payload = call_with_retry(creds, API_STATUS, deadline=deadline, label="查询签到状态")
    return parse_status(payload), ""


def cmd_status(args: argparse.Namespace) -> Result:
    try:
        creds = load_credentials()
    except AuthError as exc:
        return _auth_failure(exc, prefix="凭据不可用：")

    SCRUBBER.register(creds.access_token)
    deadline = time.monotonic() + _budget()

    try:
        status, _ = _fetch_status(creds, deadline)
    except ApiError as exc:
        return _api_failure(creds, exc, phase="查询签到状态")

    if args.fields:
        say("接口响应字段结构（仅字段名与类型，无任何值）：")
        for line in field_tree(status.raw):
            say("  " + line)
        say()

    if status.active is False:
        report = "签到活动当前未开启（接口返回 active=false），无需领取"
        LOGGER.info(report)
        return Result("activity-ended", report, EXIT_OK, detail=status_detail(status))

    if status.already_signed is None:
        report = "未能从接口返回中判定今日是否已签（可能是字段名变更，需要校准）"
        LOGGER.warning(report)
        say("  原始业务码：code=%s msg=%s" % (status.raw.get("code"), status.raw.get("msg")))
        say("  提示：用 --fields 查看完整字段名，据此修正 wb_signin.py 里的 BOOL_KEYS")
        return Result("unknown", report, EXIT_OK, detail={"code": status.raw.get("code")})

    if status.already_signed:
        report = human_report(status, prefix="今日已签到") or "今日已签到"
    else:
        report = human_report(status, prefix="今日尚未签到") or "今日尚未签到"
    say(report)

    if status.activity_name:
        say(f"活动：{status.activity_name}")
    if status.start_time is not None or status.end_time is not None:
        say(f"活动周期：{status.start_time} → {status.end_time}")
    if status.week_checkin_days is not None:
        say(f"本周已签到 {status.week_checkin_days} 天")

    return Result(
        "already" if status.already_signed else "pending",
        report,
        EXIT_OK,
        detail={"already_signed": status.already_signed, **status_detail(status)},
    )


def _budget() -> float:
    raw = os.environ.get("WORKBUDDY_BUDGET_SECONDS")
    if raw and raw.strip():
        try:
            value = int(raw.strip())
            if value > 0:
                return min(value, 540)
        except ValueError:
            pass
    return DEFAULT_BUDGET


def _api_failure(creds: Credentials, exc: ApiError, *, phase: str) -> Result:
    if exc.kind == "auth":
        message = f"{phase}：登录态失效，需要人工处理 —— {exc}"
        LOGGER.error(message)
        return Result("auth-error", message, EXIT_AUTH, notify=True)
    if exc.kind == "activity":
        message = f"{phase}：活动已结束 —— {exc}"
        LOGGER.info(message)
        return Result("activity-ended", message, EXIT_OK)
    if exc.kind == "budget":
        message = f"{phase}：时间预算耗尽，本次未完成，留给下一次兜底 —— {exc}"
        LOGGER.warning(message)
        return Result("partial", message, EXIT_PARTIAL)
    message = f"{phase}：{exc}"
    LOGGER.error(message)
    return Result("failed", message, EXIT_ERROR, notify=True)


def _do_claim(creds: Credentials, deadline: float, *, dry_run: bool) -> tuple[str, dict[str, Any]]:
    """执行签到。返回 (结果类型, 附加信息)。"""
    if dry_run:
        LOGGER.info("[dry-run] 跳过真实签到请求（真实运行时这里会调用 daily-checkin）")
        return "dry-run", {}

    payload = call_with_retry(creds, API_CLAIM, deadline=deadline, label="领取每日积分")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if not data:
        data = payload
    info = {
        "reward": _first(data, ("credits", "credit", "reward", "reward_credits", "today_credit", "amount")),
        "streak_days": _first(data, STREAK_KEYS),
        "total_credits": _first(data, CREDIT_KEYS),
        "is_streak_day": _as_bool(_first(data, ("is_streak_day", "isStreakDay"))),
        "streak_bonus_credit": _first(data, ("streak_bonus_credit", "streakBonusCredit")),
    }
    return "claimed", info


def cmd_claim(args: argparse.Namespace) -> Result:
    return _run_auto(args, force=True)


def cmd_auto(args: argparse.Namespace) -> Result:
    return _run_auto(args, force=False)


def _run_auto(args: argparse.Namespace, *, force: bool) -> Result:
    # 先做历史体检：这两步专门用来暴露「任务根本没被触发」和「已经连续多日没签上」。
    # 落盘不在这里做 —— 由 main() 在所有出口统一写，保证失败也留痕。
    prev = read_state()
    _check_history(prev)
    _alert_if_missed(prev)

    try:
        creds = load_credentials()
    except AuthError as exc:
        return _auth_failure(exc, prefix="凭据不可用：")

    SCRUBBER.register(creds.access_token)
    deadline = time.monotonic() + _budget()

    if creds.is_expired:
        message = (
            f"本地凭据已过期（到期时间 {_fmt_epoch(creds.expires_at)}），"
            "未发起任何请求。请打开 WorkBuddy 桌面端重新登录。"
        )
        LOGGER.error(message)
        return Result("auth-error", message, EXIT_AUTH, notify=True)

    status: CheckinStatus | None = None
    if not force:
        try:
            status, _ = _fetch_status(creds, deadline)
        except ApiError as exc:
            return _api_failure(creds, exc, phase="查询签到状态")

        if args.fields:
            say("接口响应字段结构（仅字段名与类型，无任何值）：")
            for line in field_tree(status.raw):
                say("  " + line)

        if status.active is False:
            report = "签到活动当前未开启（接口返回 active=false），本次不领取"
            LOGGER.info(report)
            return Result("activity-ended", report, EXIT_OK, detail=status_detail(status))

        if status.already_signed is True:
            report = human_report(status, prefix="今日已签到，跳过") or "今日已签到，跳过"
            LOGGER.info(report)
            return Result("already", report, EXIT_OK, detail=status_detail(status))

        if status.already_signed is None:
            LOGGER.warning("无法从返回中判定今日是否已签，将直接调用签到接口（该接口幂等，不会重复领取）")

    try:
        outcome, info = _do_claim(creds, deadline, dry_run=args.dry_run)
    except ApiError as exc:
        if exc.kind == "already":
            report = "签到接口返回「今日已签到」，视为成功（幂等，不会重复领取）"
            LOGGER.info(report)
            return Result("already", report, EXIT_OK, detail=status_detail(status))
        return _api_failure(creds, exc, phase="领取每日积分")

    if outcome == "dry-run":
        report = "dry-run：链路可用，未发起签到请求，未写入状态文件"
        LOGGER.info(report)
        return Result("dry-run", report, EXIT_OK, detail=status_detail(status))

    # 回读校验：确认服务端状态确实已变为「今日已签」，同时拿到签到后的真实数值
    verified: CheckinStatus | None = None
    try:
        verified, _ = _fetch_status(creds, deadline)
        if verified.already_signed is False:
            LOGGER.warning("签到接口返回成功，但回读状态仍显示未签到，请留意")
    except ApiError as exc:
        LOGGER.warning("回读校验未完成（不影响本次签到结果）：%s", exc)

    # 取数优先级：签到响应 > 回读结果 > 签到前的预检。
    # 预检值排在最后，避免报告里出现「签到前的旧累计」这类误导性数字。
    def _pick(*candidates: Any) -> Any:
        for item in candidates:
            if item is not None:
                return item
        return None

    reward = _pick(info.get("reward"), getattr(verified, "today_credit", None))
    streak = _pick(
        info.get("streak_days"),
        getattr(verified, "streak_days", None),
        getattr(status, "streak_days", None),
    )
    total = _pick(
        info.get("total_credits"),
        getattr(verified, "total_credits", None),
        getattr(status, "total_credits", None),
    )

    bits = ["签到成功"]
    if reward is not None:
        bits.append(f"+{reward} 积分")
    if info.get("is_streak_day") and info.get("streak_bonus_credit"):
        bits.append(f"连签奖励 +{info['streak_bonus_credit']} 积分")
    if streak is not None:
        bits.append(f"连续 {streak} 天")
    if total is not None:
        bits.append(f"累计 {total} 积分")
    if verified is not None and verified.already_signed is not False:
        bits.append("回读校验通过")
    report = "，".join(str(b) for b in bits)

    LOGGER.info(report)

    # 落盘内容：status_detail 取回读结果（没有回读就退回预检），再叠加签到接口
    # 的原始回报和最终采用的数值。统一由 main() 写出。
    detail: dict[str, Any] = status_detail(verified or status)
    detail.update({k: v for k, v in info.items() if v is not None})
    detail.update({"reward": reward, "streak_days": streak, "total_credits": total})

    return Result("claimed", report, EXIT_OK, detail=detail)


def _check_history(prev: dict[str, Any]) -> None:
    """运行开头的历史体检，专门用来暴露「任务根本没被触发」。

    脚本只在运行过的时候才写日志，所以整天没跑在日志里连一行都不会有。
    上一次运行留下的 last_run_ts 是唯一能证明这件事的线索。
    """
    last_run = _parse_ts(prev.get("last_run_ts"))
    if last_run is None:
        return
    gap_hours = (datetime.now() - last_run).total_seconds() / 3600.0
    if gap_hours > HEARTBEAT_GAP_HOURS:
        LOGGER.warning(
            "距上次运行已 %.1f 小时（阈值 %d 小时）。如果这台机器本来就长时间关机或睡眠，"
            "属于正常；否则请检查计划任务是否启用、电源计划是否允许唤醒定时器。",
            gap_hours, HEARTBEAT_GAP_HOURS,
        )


def _alert_if_missed(prev: dict[str, Any]) -> None:
    """连续多日没签成功就告警。一天只报一次，免得半小时一次的心跳变成刷屏。"""
    missed = _missed_days(prev.get("last_success_date"))
    if missed is None or missed < MISS_ALERT_DAYS:
        return
    if prev.get("last_miss_alert_date") == today():
        return

    message = (
        f"连续 {missed} 天没有成功签到（上次成功：{prev.get('last_success_date')}）。"
        "请检查计划任务是否正常触发、登录态是否仍有效。"
    )
    LOGGER.error(message)
    notify_failure("WorkBuddy 签到已连续多日未成功", message)
    write_state(last_miss_alert_date=today())


def _persist(*, state: str, detail: dict[str, Any] | None = None,
             exit_code: int = EXIT_OK, extra: dict[str, Any] | None = None) -> None:
    """把本次运行的结果落盘。

    由 main() 在 auto / claim 的**所有**出口统一调用，包括失败和凭据瞬时占用。
    旧版只在 already / claimed 时写，导致「跑过但失败」和「根本没跑」在
    state.json 里长得一模一样 —— 2026-10-05 那次排障就是被这一点瞒了 4 天。
    """
    now = datetime.now()
    today_str = today()
    prev = read_state()
    success = state in ("claimed", "already")

    if success:
        signed = True
    elif prev.get("date") == today_str:
        # 今天早些时候已经确认签过，别被后来的某次失败覆盖
        signed = bool(prev.get("signed"))
    else:
        signed = False

    values: dict[str, Any] = {
        "date": today_str,
        "signed": signed,
        "status": state,
        "ts": now.isoformat(timespec="seconds"),
        "source": TRIGGER,
        # 心跳三件套：下一次运行时用它判断「距上次运行过了多久」
        "last_run_ts": now.isoformat(timespec="seconds"),
        "last_result": state,
        "last_source": TRIGGER,
        "last_exit_code": exit_code,
    }

    if success:
        values["last_success_date"] = today_str
        values["consecutive_miss_days"] = 0
    else:
        missed = _missed_days(prev.get("last_success_date"))
        if missed is not None:
            values["consecutive_miss_days"] = missed

    if detail:
        for key, value in detail.items():
            if value is not None:
                values[key] = value
    if extra:
        for key, value in extra.items():
            if value is not None:
                values[key] = value

    write_state(**values)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wb_signin.py",
        description="WorkBuddy 每日自动签到脚本（纯标准库，零依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python wb_signin.py doctor   # 离线自检，不联网\n"
            "  python wb_signin.py status   # 只读查询今日签到状态\n"
            "  python wb_signin.py auto     # 查状态 → 未签才签到\n"
            "  python wb_signin.py auto --dry-run --fields\n"
        ),
    )
    parser.add_argument("command", nargs="?", default="auto",
                        choices=["doctor", "status", "claim", "auto"], help="要执行的子命令，默认 auto")
    parser.add_argument("--dry-run", action="store_true", help="演练模式：绝不调用签到接口，不写状态文件")
    parser.add_argument("--no-notify", action="store_true", help="本次运行不推送任何通知")
    parser.add_argument("--fields", action="store_true", help="额外打印接口响应的字段结构（仅字段名），用于校准")
    parser.add_argument("--json", action="store_true", help="结果以单行 JSON 输出")
    parser.add_argument("--quiet", action="store_true", help="不输出到控制台（静默运行）")
    parser.add_argument("--source", default=None,
                        help="标记本次运行的来源（如 main / poll），写入 state.json，便于事后区分触发者")
    return parser


def main(argv: list[str] | None = None) -> int:
    global TRIGGER
    args = build_parser().parse_args(argv)
    if args.source:
        TRIGGER = args.source
    setup_logging(quiet=args.quiet or args.json)

    handlers = {"doctor": cmd_doctor, "status": cmd_status, "claim": cmd_claim, "auto": cmd_auto}

    try:
        result = handlers[args.command](args)
    except AuthError as exc:
        result = _auth_failure(exc, prefix="凭据不可用：")
    except KeyboardInterrupt:
        LOGGER.warning("被用户中断")
        result = Result("aborted", "被用户中断", EXIT_PARTIAL)
    except Exception as exc:  # 兜底：任何未预期异常都要留下日志
        LOGGER.exception("未预期的异常")
        result = Result("failed", f"未预期的异常：{type(exc).__name__}: {exc}", EXIT_ERROR, notify=True)

    # 只有真正会改动服务端状态的子命令才落盘 —— status / doctor 承诺是只读的。
    # 放在这里而不是各个 return 之前，是为了让**失败和凭据瞬时占用也能留痕**：
    # 否则「跑过但失败」和「根本没跑」在 state.json 里长得一模一样。
    if args.command in ("auto", "claim") and not args.dry_run:
        _persist(state=result.status, detail=result.detail, exit_code=result.exit_code)

    if result.notify and not args.no_notify:
        notify_failure(f"WorkBuddy 签到需要处理（{result.status}）", result.message)

    if args.json:
        print(result.as_json())

    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
