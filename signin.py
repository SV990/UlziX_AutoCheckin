# -*- coding: utf-8 -*-
"""
UlziX 积分商城 每日自动签到
--------------------------------
站点: https://idc-new.ulzix.com/pointmall/signin
架构: FOSSBilling + hCaptcha 强制校验

流程:
  1. POST /api/guest/client/login 登录
  2. GET  /pointmall/signin 解析 CSRFToken 与当日签到状态
  3. 未签到 -> 打码平台求解 hCaptcha -> POST /api/client/pointmall/do_signin
  4. 结果推送飞书机器人

环境变量:
  ULZIX_EMAIL        账号邮箱          (必填)
  ULZIX_PASSWORD     账号密码          (必填)
  FEISHU_WEBHOOK     飞书机器人 webhook (必填)
  FEISHU_SECRET      飞书签名密钥       (选填, 开启签名校验时)
  CAPTCHA_PROVIDER   nopecha | yescaptcha | capsolver | twocaptcha | manual
                     manual = 不打码，仅推送提醒卡片由人工点击
  CAPTCHA_KEY        打码平台 API Key   (manual 模式外必填)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------- 基础配置

BASE_URL = "https://idc-new.ulzix.com"
LOGIN_API = f"{BASE_URL}/api/guest/client/login"
SIGNIN_PAGE = f"{BASE_URL}/pointmall/signin"
DO_SIGNIN_API = f"{BASE_URL}/api/client/pointmall/do_signin"
GET_CAPTCHA_API = f"{BASE_URL}/api/client/pointmall/get_captcha"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    return datetime.now(CST)


def log(msg: str, level: str = "INFO") -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S}] [{level}] {msg}", flush=True)


def env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def load_dotenv(path: str = ".env") -> None:
    """本地调试用：读取同目录 .env（不覆盖已存在的环境变量）"""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- IP 归属地

# api.ip.sb 返回示例:
# {"ip":"20.3.221.188","country_code":"US","country":"United States",
#  "region":"Washington","city":"Quincy","asn_organization":"Microsoft Corporation",...}
IPINFO_API = "https://api.ip.sb/geoip"


def ip_location() -> dict:
    """查询当前出口 IP 及其归属地（直连查询，无代理）。失败返回空 dict。"""
    try:
        r = requests.get(IPINFO_API, timeout=15).json()
        return r if r.get("ip") else {}
    except Exception:
        return {}


def fmt_location(info: dict) -> str:
    """拼接 country / region / city 为可读文本（自动去重 + 斜线分隔）"""
    parts = []
    for p in (info.get("country", ""), info.get("region", ""), info.get("city", "")):
        if p and (not parts or p != parts[-1]):
            parts.append(p)
    return " / ".join(parts) if parts else ""


def net_info_rows() -> tuple[str, str, str]:
    """生成 IP / 位置 / ISP 三个字段值，失败时降级为「未知」"""
    info = ip_location()
    ip = info.get("ip", "未知")
    loc = fmt_location(info) or "未知"
    isp = info.get("asn_organization") or "未知"
    return ip, loc, isp


def net_section() -> list[dict]:
    """生成通知卡片底部的「IP / 位置 / ISP / 来源」两列分区"""
    ip, loc, isp = net_info_rows()
    return [
        {"kind": "fields", "fields": [("IP", ip), ("位置", loc)]},
        {"kind": "fields", "fields": [("ISP", isp), ("来源", "api.ip.sb")]},
    ]


def captcha_field(solver) -> dict | None:
    """生成「打码平台 / 打码余额」两列字段；solver 不存在或余额查询失败返回 None。
    余额 <300 点（≈10 天）时自动追加 ⚠️ 提示。"""
    if solver is None:
        return None
    bal = solver.balance()
    if bal is None:
        return {"kind": "fields", "fields": [
            ("打码平台", solver.provider),
            ("打码余额", "查询失败"),
        ]}
    warn = " ⚠️ 不足 10 天" if bal < 300 else ""
    return {"kind": "fields", "fields": [
        ("打码平台", solver.provider),
        ("打码余额", f"{bal} 点{warn}"),
    ]}


# ---------------------------------------------------------------- 飞书通知

class Feishu:
    def __init__(self, webhook: str, secret: str = ""):
        self.webhook = webhook
        self.secret = secret

    def _sign(self, ts: int) -> str:
        raw = f"{ts}\n{self.secret}".encode("utf-8")
        return base64.b64encode(hmac.new(raw, digestmod=hashlib.sha256).digest()).decode()

    def send_card(self, title: str, color: str, sections: list[dict],
                  note: str = "", button: tuple[str, str] | None = None) -> bool:
        """
        sections: 列表，每项是一个 dict:
          {"kind": "fields",  "fields": [(label, value), ...]}  → 两列等宽布局
          {"kind": "heading", "heading": "### 💪 状态"}          → 段落小标题
          {"kind": "text",    "text": "正文 markdown"}            → 正文段落
          {"kind": "divider"}                                     → 分割线
        note: 卡片底部时间戳 / 来源说明
        button: (按钮文案, 跳转 URL)
        """
        if not self.webhook:
            log("未配置飞书 webhook，跳过通知", "WARN")
            return False

        elements: list[dict] = []
        for sec in sections:
            kind = sec.get("kind")
            if kind == "fields":
                elements.append({
                    "tag": "div",
                    "fields": [
                        {"is_short": True,
                         "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}}
                        for k, v in sec["fields"]
                    ],
                })
            elif kind == "heading":
                elements.append({"tag": "div",
                                 "text": {"tag": "lark_md", "content": sec["heading"]}})
            elif kind == "text":
                elements.append({"tag": "div",
                                 "text": {"tag": "lark_md", "content": sec["text"]}})
            elif kind == "divider":
                elements.append({"tag": "hr"})

        if button:
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": button[0]},
                    "url": button[1],
                    "type": "primary",
                }],
            })
        if note:
            elements.append({"tag": "hr"})
            elements.append({"tag": "note",
                             "elements": [{"tag": "plain_text", "content": note}]})

        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": title},
                           "template": color},
                "elements": elements,
            },
        }
        if self.secret:
            ts = int(time.time())
            payload["timestamp"] = str(ts)
            payload["sign"] = self._sign(ts)

        try:
            r = requests.post(self.webhook, json=payload, timeout=20)
            ok = r.json().get("code", -1) == 0
            log(f"飞书通知{'已送达' if ok else '发送失败: ' + r.text}")
            return ok
        except Exception as e:
            log(f"飞书通知异常: {e}", "ERROR")
            return False


# ---------------------------------------------------------------- 打码平台

class CaptchaError(Exception):
    pass


class CaptchaSolver:
    """hCaptcha 求解器，支持 YesCaptcha / CapSolver / 2Captcha"""

    def __init__(self, provider: str, api_key: str, timeout: int = 180):
        self.provider = (provider or "yescaptcha").lower()
        self.api_key = api_key
        self.timeout = timeout
        if not api_key:
            raise CaptchaError(
                "未配置 CAPTCHA_KEY。该站点签到强制校验 hCaptcha，必须接入打码平台。"
            )

    def balance(self) -> float | None:
        """查询打码平台余额，失败返回 None（不影响主流程）"""
        try:
            if self.provider == "yescaptcha":
                r = requests.post("https://api.yescaptcha.com/getBalance",
                                  json={"clientKey": self.api_key}, timeout=15).json()
                return r.get("balance") if not r.get("errorId") else None
            if self.provider == "capsolver":
                r = requests.post("https://api.capsolver.com/getBalance",
                                  json={"clientKey": self.api_key}, timeout=15).json()
                return r.get("balance") if not r.get("errorId") else None
            if self.provider == "twocaptcha":
                r = requests.get("https://2captcha.com/res.php",
                                 params={"key": self.api_key, "action": "getbalance",
                                         "json": 1}, timeout=15).json()
                return float(r["request"]) if r.get("status") == 1 else None
        except Exception:
            return None
        return None

    def solve(self, sitekey: str, page_url: str) -> str:
        log(f"调用打码平台 [{self.provider}] 求解 hCaptcha…")
        t0 = time.time()
        if self.provider == "nopecha":
            token = self._nopecha(sitekey, page_url)
        elif self.provider == "twocaptcha":
            token = self._two_captcha(sitekey, page_url)
        else:
            token = self._anticaptcha_like(sitekey, page_url)
        log(f"打码成功，耗时 {time.time() - t0:.1f}s，token 长度 {len(token)}")
        return token

    # --- NopeCHA（有免费额度，Token API 每次 5 credits）---
    def _nopecha(self, sitekey: str, page_url: str) -> str:
        api = "https://api.nopecha.com/token/"
        body = {"key": self.api_key, "type": "hcaptcha",
                "sitekey": sitekey, "url": page_url, "useragent": UA}

        r = requests.post(api, json=body, timeout=30).json()
        if r.get("error") is not None:
            raise CaptchaError(
                f"创建任务失败[{r.get('error')}]: {r.get('message') or r}")
        job_id = r.get("data")
        if not job_id:
            raise CaptchaError(f"未返回任务 ID: {r}")
        log(f"打码任务已创建: {str(job_id)[:24]}…")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(5)
            res = requests.get(api, params={"key": self.api_key, "id": job_id},
                               timeout=30).json()
            err = res.get("error")
            if err is not None:
                # error 14 = Incomplete job，继续轮询
                if err == 14:
                    continue
                raise CaptchaError(f"求解失败[{err}]: {res.get('message') or res}")
            token = res.get("data")
            if token:
                return token
        raise CaptchaError(f"打码超时（{self.timeout}s）")

    # --- YesCaptcha / CapSolver 共用 AntiCaptcha 风格协议 ---
    def _anticaptcha_like(self, sitekey: str, page_url: str) -> str:
        host = {"yescaptcha": "https://api.yescaptcha.com",
                "capsolver": "https://api.capsolver.com"}.get(self.provider)
        if not host:
            raise CaptchaError(f"不支持的打码平台: {self.provider}")

        task = {"type": "HCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": sitekey}
        r = requests.post(f"{host}/createTask",
                          json={"clientKey": self.api_key, "task": task},
                          timeout=30).json()
        if r.get("errorId"):
            raise CaptchaError(f"创建任务失败: {r.get('errorDescription') or r}")
        task_id = r.get("taskId")
        log(f"打码任务已创建: {task_id}")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(5)
            res = requests.post(f"{host}/getTaskResult",
                                json={"clientKey": self.api_key, "taskId": task_id},
                                timeout=30).json()
            if res.get("errorId"):
                raise CaptchaError(f"求解失败: {res.get('errorDescription') or res}")
            if res.get("status") == "ready":
                sol = res.get("solution") or {}
                token = sol.get("gRecaptchaResponse") or sol.get("token") or ""
                if not token:
                    raise CaptchaError(f"返回结果无 token: {res}")
                return token
        raise CaptchaError(f"打码超时（{self.timeout}s）")

    # --- 2Captcha 经典协议 ---
    def _two_captcha(self, sitekey: str, page_url: str) -> str:
        r = requests.post("https://2captcha.com/in.php",
                          data={"key": self.api_key, "method": "hcaptcha",
                                "sitekey": sitekey, "pageurl": page_url, "json": 1},
                          timeout=30).json()
        if r.get("status") != 1:
            raise CaptchaError(f"创建任务失败: {r}")
        task_id = r["request"]

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(5)
            res = requests.get("https://2captcha.com/res.php",
                               params={"key": self.api_key, "action": "get",
                                       "id": task_id, "json": 1},
                               timeout=30).json()
            if res.get("status") == 1:
                return res["request"]
            if res.get("request") != "CAPCHA_NOT_READY":
                raise CaptchaError(f"求解失败: {res}")
        raise CaptchaError(f"打码超时（{self.timeout}s）")


# ---------------------------------------------------------------- 签到主体

class UlzixSigner:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": BASE_URL,
        })
        self.csrf = ""

    # ---- 工具 ----
    def _get_csrf(self, html: str) -> str:
        m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
        return m.group(1) if m else ""

    # ---- 步骤 1：登录 ----
    def login(self) -> None:
        log("获取登录页 CSRF…")
        html = self.s.get(f"{BASE_URL}/login", timeout=30).text
        self.csrf = self._get_csrf(html)
        if not self.csrf:
            raise RuntimeError("无法解析登录页 CSRFToken")

        log(f"登录账号 {self.email} …")
        r = self.s.post(
            LOGIN_API,
            data={"email": self.email, "password": self.password,
                  "CSRFToken": self.csrf},
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Referer": f"{BASE_URL}/login"},
            timeout=30,
        )
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"登录响应非 JSON（HTTP {r.status_code}）")

        if data.get("error"):
            raise RuntimeError(f"登录失败: {data['error'].get('message')}")
        res = data.get("result") or {}
        log(f"登录成功: {res.get('name', '').strip()} (ID={res.get('id')})")

    # ---- 步骤 2：读取签到页状态 ----
    def fetch_status(self) -> dict:
        log("读取签到页状态…")
        html = self.s.get(SIGNIN_PAGE, timeout=30,
                          headers={"Referer": BASE_URL}).text

        if "/login" in html and "csrf-token" not in html:
            raise RuntimeError("会话失效，未能进入签到页")

        csrf = self._get_csrf(html)
        if csrf:
            self.csrf = csrf

        today = now_cst().strftime("%Y-%m-%d")
        signed_dates = set(re.findall(r"signedDates\.add\('(\d{4}-\d{2}-\d{2})'\)", html))

        m = re.search(r'class="[^"]*points-format[^"]*"\s+data-points="(\d+)"', html)
        points = int(m.group(1)) if m else -1

        m = re.search(r'已连续签到\s*<span[^>]*>(\d+)</span>', html)
        streak = int(m.group(1)) if m else -1

        m = re.search(r'data-sitekey="([0-9a-fA-F-]{36})"', html)
        sitekey = m.group(1) if m else ""

        already = (today in signed_dates) or ("今日已签到" in html)

        log(f"今日={today} 已签到={already} 当前积分={points} 连续={streak}天")
        return {"already": already, "points": points, "streak": streak,
                "sitekey": sitekey, "today": today,
                "signed_count": len(signed_dates)}

    # ---- 步骤 3：验证码模式 ----
    def captcha_mode(self) -> dict:
        r = self.s.get(GET_CAPTCHA_API, params={"CSRFToken": self.csrf},
                       headers={"Referer": SIGNIN_PAGE}, timeout=30).json()
        return r.get("result") or {}

    # ---- 步骤 4：提交签到 ----
    def do_signin(self, captcha_token: str) -> dict:
        behavior = json.dumps({
            "mouse_distance": random.randint(900, 4200),
            "click_interval": random.randint(2600, 9000),
            "page_duration": random.randint(11000, 27000),
        })
        payload = {"captcha": captcha_token, "CSRFToken": self.csrf,
                   "behavior": behavior}

        log("提交签到请求…")
        r = self.s.post(DO_SIGNIN_API, params={"CSRFToken": self.csrf},
                        json=payload,
                        headers={"Content-Type": "application/json",
                                 "Referer": SIGNIN_PAGE,
                                 "X-Requested-With": "XMLHttpRequest"},
                        timeout=60)
        try:
            return r.json()
        except Exception:
            raise RuntimeError(f"签到响应非 JSON（HTTP {r.status_code}）: {r.text[:200]}")


# ---------------------------------------------------------------- 编排

def main() -> int:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    email = env("ULZIX_EMAIL")
    password = env("ULZIX_PASSWORD")
    webhook = env("FEISHU_WEBHOOK")
    secret = env("FEISHU_SECRET")
    provider = env("CAPTCHA_PROVIDER", "yescaptcha")
    cap_key = env("CAPTCHA_KEY")

    fs = Feishu(webhook, secret)
    stamp = now_cst().strftime("%Y-%m-%d %H:%M:%S")
    note = f"🕐 {stamp} | UlziX 自动签到"

    if not email or not password:
        log("缺少 ULZIX_EMAIL / ULZIX_PASSWORD", "ERROR")
        fs.send_card("UlziX 云服务 · 配置错误", "red", [
            {"kind": "heading", "heading": "### ❌ 状态"},
            {"kind": "text", "text": "缺少账号或密码环境变量"},
            {"kind": "divider"},
            *net_section(),
        ], note)
        return 1

    solver: CaptchaSolver | None = None

    try:
        # 随机延迟，避免整点集中请求
        delay = int(env("RANDOM_DELAY", "0"))
        if delay > 0:
            wait = random.randint(0, delay)
            log(f"随机延迟 {wait}s …")
            time.sleep(wait)

        signer = UlzixSigner(email, password)
        signer.login()
        st = signer.fetch_status()

        if st["already"]:
            log("今日已签到，无需重复操作")
            if env("NOTIFY_WHEN_SIGNED", "true").lower() in ("1", "true", "yes"):
                # 今日已签到也展示打码余额，方便提前知道何时充值；
                # 仅在配置了 key 且非 manual 模式时才查询
                cap_field = None
                if cap_key and provider != "manual":
                    try:
                        _sol = CaptchaSolver(provider, cap_key, timeout=15)
                        cap_field = captcha_field(_sol)
                        log(f"打码余额: {cap_field['fields'][1][1]}")
                    except Exception as e:
                        log(f"查询打码余额失败: {e}", "WARN")

                sections = [
                    {"kind": "fields", "fields": [("日期", st["today"]),
                                                   ("时间", stamp.split(" ", 1)[1])]},
                    {"kind": "fields", "fields": [("账号", email),
                                                   ("当前积分", str(st["points"]))]},
                ]
                if cap_field:
                    sections.append(cap_field)
                sections += [
                    {"kind": "divider"},
                    {"kind": "heading", "heading": "### 💪 状态"},
                    {"kind": "text", "text": f"今日已完成签到，连续 {st['streak']} 天"},
                    {"kind": "divider"},
                    *net_section(),
                ]
                fs.send_card("UlziX 云服务 · 今日已打卡", "blue", sections, note)
            else:
                log("静默模式（兜底运行），跳过已签到通知")
            return 0

        # 确认验证码模式
        cap = signer.captcha_mode()
        mode = cap.get("mode", "hcaptcha")
        sitekey = cap.get("site_key") or st["sitekey"]
        log(f"验证码模式={mode} sitekey={sitekey}")

        # 提醒模式：不打码，仅推送带跳转按钮的卡片，由人工点一下完成签到
        if provider == "manual":
            log("提醒模式：推送手动签到提醒")
            fs.send_card("UlziX 云服务 · 待打卡", "orange", [
                {"kind": "fields", "fields": [("日期", st["today"]),
                                               ("时间", stamp.split(" ", 1)[1])]},
                {"kind": "fields", "fields": [("账号", email),
                                               ("当前积分", str(st["points"]))]},
                {"kind": "divider"},
                {"kind": "heading", "heading": "### 💪 状态"},
                {"kind": "text", "text":
                    f"尚未签到，请手动完成（断签会重置连续天数，当前 {st['streak']} 天）"},
                {"kind": "divider"},
                *net_section(),
            ], note, button=("前往签到", SIGNIN_PAGE))
            return 0

        if mode in ("hcaptcha", "turnstile", "recaptcha"):
            solver = CaptchaSolver(provider, cap_key,
                                   timeout=int(env("CAPTCHA_TIMEOUT", "180")))
            token = solver.solve(sitekey, SIGNIN_PAGE)
        elif mode == "none":
            token = ""
        else:
            raise RuntimeError(f"暂不支持的验证码模式: {mode}")

        # 提交签到，失败重试一次（token 过期 / 偶发拒绝）
        resp = signer.do_signin(token)
        if resp.get("error"):
            msg = resp["error"].get("message", "")
            log(f"首次签到失败: {msg}", "WARN")
            if any(k in msg for k in ("验证码", "captcha", "Captcha")) and solver:
                log("重新求解验证码后重试…")
                token = solver.solve(sitekey, SIGNIN_PAGE)
                resp = signer.do_signin(token)

        if resp.get("error"):
            raise RuntimeError(resp["error"].get("message", "未知错误"))

        result = resp.get("result")
        detail = result if isinstance(result, dict) else {}

        # 实测返回: {"points":3,"streak_days":3,"level_bonus":0,"milestone_rewards":[]}
        earned = detail.get("points", detail.get("points_earned", 0)) or 0
        streak = (detail.get("streak_days") or detail.get("streak")
                  or detail.get("continuous_days") or st["streak"] + 1)
        bonus = detail.get("level_bonus", 0) or 0
        milestones = detail.get("milestone_rewards") or []
        # 接口不返回总积分，用签到前余额累加得出
        total = detail.get("total_points") or detail.get("balance")
        if not total:
            total = st["points"] + earned + bonus if st["points"] >= 0 else "?"

        log(f"签到成功! 获得={earned} 等级加成={bonus} 连续={streak}天 总积分={total}")
        log(f"原始返回: {json.dumps(resp, ensure_ascii=False)[:400]}")

        earned_text = f"+{earned} 点" + (f"（含等级加成 +{bonus}）" if bonus else "")

        # 通知里独立展示「打码平台 + 余额」，方便一眼看出还能跑多少天
        status_lines = [f"签到成功，获得 {earned_text}"]
        cap_field = captcha_field(solver)
        if cap_field:
            log(f"打码余额: {cap_field['fields'][1][1]}")

        sections = [
            {"kind": "fields", "fields": [("日期", st["today"]),
                                           ("时间", stamp.split(" ", 1)[1])]},
            {"kind": "fields", "fields": [("账号", email),
                                           ("获得积分", earned_text)]},
        ]
        if cap_field:
            sections.append(cap_field)
        sections += [
            {"kind": "divider"},
            {"kind": "heading", "heading": "### 💪 状态"},
            {"kind": "text", "text": "\n".join(status_lines)},
        ]
        if milestones:
            sections += [
                {"kind": "divider"},
                {"kind": "text", "text":
                    "**里程碑奖励**：" + json.dumps(milestones, ensure_ascii=False)},
            ]
        sections += [
            {"kind": "divider"},
            *net_section(),
        ]

        fs.send_card("UlziX 云服务 · 打卡成功", "green", sections, note)
        return 0

    except CaptchaError as e:
        log(str(e), "ERROR")
        cap_field = captcha_field(solver)
        cap_sections = []
        if cap_field:
            cap_sections.append(cap_field)
            cap_sections.append({"kind": "divider"})
        fs.send_card("UlziX 云服务 · 打卡失败 · 验证码", "red", [
            {"kind": "fields", "fields": [("账号", email),
                                           ("时间", stamp.split(" ", 1)[1])]},
            *cap_sections,
            {"kind": "heading", "heading": "### ❌ 状态"},
            {"kind": "text", "text": f"**原因**：{e}\n**建议**：检查打码平台余额与 CAPTCHA_KEY 配置"},
            {"kind": "divider"},
            *net_section(),
        ], note)
        return 1

    except Exception as e:
        log(f"签到异常: {e}", "ERROR")
        fs.send_card("UlziX 云服务 · 打卡失败", "red", [
            {"kind": "fields", "fields": [("账号", email),
                                           ("时间", stamp.split(" ", 1)[1])]},
            {"kind": "divider"},
            {"kind": "heading", "heading": "### ❌ 状态"},
            {"kind": "text", "text": f"**错误**：{str(e)[:400]}"},
            {"kind": "divider"},
            *net_section(),
        ], note)
        return 1


if __name__ == "__main__":
    sys.exit(main())
