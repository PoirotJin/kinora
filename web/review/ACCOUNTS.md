# MotionFarm 账户登录

网页入口：http://127.0.0.1:8765/。仅公司员工账户可登录，无公开注册。旧访问码、`#token=` 链接和 `access_token` 文件不再用于认证。

首个账户：`jfc`，永远是管理员。服务启动时会把 `jfc` 重新设为管理员并禁止停用；降级只能改服务器代码。临时密码文件：`outputs/text_motion_farm/review/initial-jfc-login.txt`（权限 600）。用户首次登录后必须修改密码。新密码 12–256 个字符。

密码使用随机盐和 scrypt 哈希保存在 `accounts.sqlite3`。同一时刻只运行一次 scrypt，多余登录返回繁忙。会话 8 小时，库中只存令牌摘要。浏览器 Cookie：`HttpOnly; SameSite=Strict`；启用 TLS 后增加 `Secure` 和 HSTS。退出登录撤销当前会话，修改或重置密码、停用账户会撤销该用户全部会话。审核人由后端按当前账户写入。

登录和改密按 IP、账户限流：10 分钟内每账户 12 次、每 IP 20 次。写请求必须带来源白名单中的 `Origin`，以及 `X-MotionFarm-Request` 与 JSON。浏览器来源默认 `http://127.0.0.1:8765`，公网部署时改为 `https://你的域名`。

## 角色

管理员：生产、调度、审核、监控、归档导出、建用户、停用/启用、踢会话、重置密码。不能停用 `jfc`。
普通用户：提交并管理自己的定制作业，浏览共享画廊。画廊里可用「我的作业」筛出自己提交的动作。

网页只能创建普通用户。CLI 可指定 `--role`：

```sh
timeout 10s bash python3 scripts/review_auth.py alice --credentials-file outputs/text_motion_farm/review/initial-alice-login.txt
```

## 普通用户 Qwen 额度

按现网 `qwen3.8-flash` 统计：一次定制约 1,300–2,400 chat token + 约 40 embedding token，按 2,000 token/次估算。

| 窗口 | 上限 | 大约可提交 |
|---|---|---|
| 近 5 小时 | 80,000 token | 30–40 次定制（含重试） |
| 近 7 天 | 400,000 token | 约 150 次定制 |

管理员不限额。超限时提交和文本转换会失败。额度记在 `api_usage.sqlite3` 的 `spend` 表，按账户累计。

## TLS（公网前必须启用）

当前内网默认 HTTP。放到云服务器前：

```sh
timeout 20s bash python3 scripts/review_control.py generate-tls --host 你的域名或公网IP
timeout 10s bash python3 scripts/review_control.py stop
timeout 10s bash python3 scripts/review_control.py start --host 你的IP --origins https://你的域名
```

若已有正式证书，放到 `outputs/text_motion_farm/review/tls/cert.pem` 与 `key.pem`（权限 600）。`start` 发现证书会自动走 HTTPS，Cookie 带 Secure。不要只加反向代理而不配来源白名单。
