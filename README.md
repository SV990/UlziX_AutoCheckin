# UlziX 积分商城 · 每日自动签到

针对 `https://idc-new.ulzix.com/pointmall/signin` 的全自动签到脚本，支持本地运行与 GitHub Actions 定时托管，签到结果通过飞书机器人推送。

---

## 一、站点技术分析结论

| 项目 | 结论 |
|---|---|
| 站点框架 | FOSSBilling（主题 UlziX / huraga） |
| 登录接口 | `POST /api/guest/client/login`，字段 `email` / `password` / `CSRFToken` |
| 签到接口 | `POST /api/client/pointmall/do_signin`，JSON 提交 |
| 状态来源 | 签到页 HTML 内嵌 `signedDates` 集合、积分、连续天数 |
| **验证码** | **hCaptcha 强制校验**，sitekey `1abc99a0-9922-4b3e-9268-2e1e6398d284` |
| 行为检测 | 提交时附带 `behavior`（鼠标轨迹/点击间隔/停留时长），脚本已生成合理随机值 |

### 关于验证码的重要说明

已实测验证：

- 直接以空验证码提交 → 服务端返回 `需要验证码。`（code 1000），**服务端强制校验**
- Playwright 无头浏览器点击 hCaptcha → **弹出图片挑战**，无法被动通过
- Playwright 有头浏览器点击 hCaptcha → **同样弹出图片挑战**

因此该站点**无法通过纯浏览器模拟实现全自动签到**，必须接入打码平台求解 hCaptcha。

好消息是：打码平台只需要 `sitekey` + 页面 URL，不需要浏览器环境。所以本脚本使用纯 `requests` 实现，**不依赖 Playwright/Chromium**，在 GitHub Actions 上运行仅需十几秒，资源占用极低。

---

## 二、选择验证码方案

脚本支持 5 种 `CAPTCHA_PROVIDER`，按成本排序：

| 方案 | 成本 | 全自动 | 说明 |
|---|---|:---:|---|
| **`yescaptcha`** ⭐ | **30 点/次** | ✅ | **当前使用**。国内平台，支付宝/微信充值，实测 20 秒出 token |
| `capsolver` | $0.8/千次 | ✅ | 国际平台，识别率高 |
| `twocaptcha` | $2.99/千次 | ✅ | 老牌平台，hCaptcha 耗时偏长 |
| `nopecha` | 需付费套餐 | ✅ | 实测免费账号调 Token API 返回 `Feature unavailable for current plan`，免费额度**不含** hCaptcha token |
| `manual` | **完全免费** | ❌ | 不打码，推送带「前往签到」按钮的飞书卡片，手动点一下（约 10 秒） |

### 当前方案：YesCaptcha（已实测跑通）

1. 注册：<https://yescaptcha.com/i/register>
2. [控制台](https://yescaptcha.com/dashboard.html) 复制 **clientKey**
3. 设置 `CAPTCHA_PROVIDER=yescaptcha`、`CAPTCHA_KEY=你的key`

**实测成本**：单次签到消耗 **30 点**，注册赠送 1500 点 ≈ **可用 50 天**。
后续充值 ¥10 约 10000 点 ≈ 330 天，折合**一年不到 12 块钱**。

脚本会在签到成功的卡片上显示剩余额度，**低于 300 点（≈10 天）时自动预警**，
不会出现余额耗尽却毫无察觉的情况。

### 零成本兜底：manual 提醒模式

若不想花钱，设 `CAPTCHA_PROVIDER=manual`，无需任何 Key：

- 今日**未签到** → 橙色卡片 + 「前往签到」跳转按钮，点进去手动完成
- 今日**已签到** → 蓝色卡片确认

不是全自动，但彻底解决「忘记签到导致断签」的问题。

### 关于"完全免费且全自动"

不存在。hCaptcha 图片挑战需要 AI 模型识别，算力有成本：

- NopeCHA 宣称的免费额度**不覆盖** hCaptcha Token API（已实测确认报错）
- 开源方案 `hcaptcha-challenger` 需本地跑视觉模型，Actions 上耗时长、依赖重、
  识别率不稳定，维护成本远高于每年十几块的打码费

---

## 三、本地测试

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 创建配置
cp .env.example .env
#    编辑 .env，填入 CAPTCHA_KEY

# 3. 运行
python signin.py
```

**真实运行日志**（2026-08-03 首次跑通）：

```
[14:40:46] [INFO] 获取登录页 CSRF…
[14:40:49] [INFO] 登录成功: xxx
[14:40:50] [INFO] 今日=2026-08-03 已签到=False 当前积分=100连续=10天
[14:40:50] [INFO] 验证码模式=hcaptcha sitekey=xxxxxxxxxxxxxxxxxxxxxx
[14:40:50] [INFO] 调用打码平台 [yescaptcha] 求解 hCaptcha…
[14:40:52] [INFO] 打码任务已创建: xxxxxxxxxxxxxxxxxxxxxx
[14:41:10] [INFO] 打码成功，耗时 19.7s，token 长度 2620
[14:41:10] [INFO] 提交签到请求…
[14:41:11] [INFO] 签到成功! 获得=x等级加成=x 连续=x天 总积分=xx
[14:41:11] [INFO] 飞书通知已送达
```

重复运行会自动跳过，**不浪费打码额度**：

```
[14:41:44] [INFO] 今日=2026-08-03 已签到=True 当前积分=xx 连续=xx天
[14:41:44] [INFO] 今日已签到，无需重复操作
```

签到接口真实返回结构：

```json
{"result":{"points":x,"streak_days":x,"level_bonus":x,"milestone_rewards":[]},"error":null}
```

---

## 四、部署到 GitHub Actions

### 1. 推送代码到私有仓库

```bash
git init
git add .
git commit -m "feat: ulzix 自动签到"
git remote add origin git@github.com:你的用户名/仓库名.git
git push -u origin main
```

> **务必使用私有仓库**。`.env` 已在 `.gitignore` 中，不会被提交，但仍建议私有化。

### 2. 配置 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret 名称 | 是否必需 | 值 |
|---|:---:|---|
| `ULZIX_EMAIL` | ✅ | `xxxxxxx` |
| `ULZIX_PASSWORD` | ✅ | 你的账号密码 |
| `FEISHU_WEBHOOK` | ✅ | `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `CAPTCHA_KEY` | ✅ | YesCaptcha 的 clientKey（用 `manual` 模式则不需要） |
| `FEISHU_SECRET` | 选填 | 仅当飞书机器人开启签名校验时填写 |

`CAPTCHA_PROVIDER` 默认就是 `yescaptcha`，无需额外配置。
若要切其他平台，在同页面的 **Variables** 标签新增该变量即可。

### 3. 验证

进入 **Actions → UlziX 每日自动签到 → Run workflow** 手动触发一次，确认飞书收到通知。

### 4. 执行时间

> **每月自动打卡上限：10 天**。GitHub Actions 运行器无状态，无法在脚本内可靠计数，
> 因此直接用 cron 的「日」字段锁定触发日期（每隔约 3 天一次：每月 1/4/7/10/13/16/19/22/25/28 号）。
> 想在其他日子补卡，用 **Actions → Run workflow** 手动触发即可（手动触发不受 10 天限制）。

| 时间（北京） | 触发日 | 说明 |
|---|---|---|
| 09:00 | 上述 10 天 | 主签到，无论成功失败都推送通知 |
| 20:00 | 上述 10 天 | 兜底重试，若当天已签到则静默不推送 |

脚本内置 0–600 秒随机延迟，避开 GitHub 整点任务拥堵。

> GitHub 定时任务在高峰期可能延迟 5–20 分钟，属正常现象；双时段设计已覆盖此风险。
> 如需调整天数，改 `.github/workflows/signin.yml` 里两条 cron 的「日」字段即可（务必两条同步改）。

---

## 五、飞书通知样式

所有卡片统一采用 **两列等宽字段** + **状态段落** + **底部时间戳** 的版式（直连查询 `api.ip.sb` 填充 IP / 位置 / ISP）。

### 卡片通用骨架

```
┌─────────────────────────────────────────────┐
│ 标题 + 颜色条                                 │
├─────────────────────────────────────────────┤
│ **日期**         │ **时间**                  │
│ 2026-08-03       │ 09:00:12                  │
│ **账号**         │ **<积分相关>**            │
│ 'xxxxxxxxx'│ xxx 点                     │
├─────────────────────────────────────────────┤
│ ### 💪 状态                                  │
│ <本场景的一句话描述>                         │
├─────────────────────────────────────────────┤
│ **IP**             │ **位置**                │
│ 1.1.1.1            │ 1.11.           │
│ **ISP**            │ **来源**                │
│ FDCservers.net     │ api.ip.sb               │
├─────────────────────────────────────────────┤
│ 🕐 2026-08-03 09:00:12 | UlziX 自动签到     │
└─────────────────────────────────────────────┘
```

### 各场景差异

| 场景 | 标题 | 颜色 | 状态行内容 | 额外元素 |
|---|---|---|---|---|
| 签到成功 | **UlziX 云服务 · 打卡成功** | 🟩 绿 | `签到成功，获得 +N 点（含等级加成 +B）` | **打码平台 / 打码余额**（两列；<300 时自动 ⚠️） |
| 今日已签到 | **UlziX 云服务 · 今日已打卡** | 🟦 蓝 | `今日已完成签到，连续 N 天` | — |
| 待手动签到 | **UlziX 云服务 · 待打卡** | 🟧 橙 | `尚未签到，请手动完成（断签会重置连续天数…）` | **「前往签到」跳转按钮** |
| 验证码失败 | **UlziX 云服务 · 打卡失败 · 验证码** | 🟥 红 | `原因：…  建议：检查打码平台余额与 CAPTCHA_KEY 配置` | **打码平台 / 打码余额**（便于一眼判断是否余额耗尽） |
| 其他异常 | **UlziX 云服务 · 打卡失败** | 🟥 红 | `错误：…` | — |
| 配置缺失 | **UlziX 云服务 · 配置错误** | 🟥 红 | `缺少账号或密码环境变量` | — |

> **IP 信息说明**：脚本无代理，直连查询 `api.ip.sb/geoip` 得到本机出口 IP 与归属地。该字段便于排查"为什么本地/海外跑不通"——例如 GitHub Actions 跑出美国机房 IP 就说明 Cloudflare 可能已放行；遇到中国机房 IP 被 Cloudflare 拦截也可一眼识别。

---

## 六、故障排查

| 现象 | 原因与处理 |
|---|---|
| `登录失败: ...` | 密码错误，或账号被风控；先手动网页登录确认 |
| `需要验证码。` | 打码返回的 token 已过期或无效；脚本会自动重试一次，仍失败则检查打码平台余额 |
| `创建任务失败` | 打码平台 Key 错误或余额不足 |
| `打码超时` | 平台繁忙，可调大 `CAPTCHA_TIMEOUT` |
| Actions 中报连接超时 / 403 | 站点在 Cloudflare 后面，可能拦截 GitHub 机房 IP。解决方案见下 |

### 若 GitHub Actions 的 IP 被 Cloudflare 拦截

这是海外机房 IP 的常见问题。备选部署方式（脚本无需改动，纯 requests 依赖极轻）：

1. **自有服务器 / NAS**：`crontab -e` 添加
   `0 9 * * * cd /path/to/签到 && /usr/bin/python3 signin.py >> signin.log 2>&1`
2. **青龙面板**：拉取仓库后添加定时任务 `9 0 * * *`
3. **腾讯云函数 / 阿里云函数计算**：国内 IP，定时触发器

---

## 七、文件说明

```
签到/
├── signin.py                    # 主脚本（登录 → 状态检测 → 打码 → 签到 → 通知）
├── requirements.txt             # 仅依赖 requests
├── .env.example                 # 配置模板
├── .gitignore                   # 忽略 .env 等敏感文件
├── README.md
└── .github/workflows/signin.yml # Actions 定时工作流
```
