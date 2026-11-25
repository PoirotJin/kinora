# 服务端入门

下载 [Kinora Server 1.0.0-beta.4 源码包](https://github.com/PoirotJin/kinora/releases/download/kinora-v1.0.0-beta.4-server/kinora-server-1.0.0-beta.4.zip)（稳定基线为 [0.5.2](https://github.com/PoirotJin/kinora/releases/download/kinora-server-v0.5.2/kinora-server-0.5.2.zip)），解压后进入 `kinora-server-1.0.0-beta.4/`。包内包含完整服务端源码、Gadget 0.9.1、依赖清单、`deploy/`（含 `nginx.conf`）、`deployment/` 服务模板和验收工具。

## 先启动本机服务

使用 Linux 或 macOS，以及支持 scrypt 的 Python 3.10–3.13。完整 GPU 部署以 Linux 为准。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p runtime/demo/review
.venv/bin/python scripts/review_auth.py admin --role admin \
  --root runtime/demo/review --credentials-file runtime/demo/first-login.txt
.venv/bin/python scripts/review_server.py --host 127.0.0.1 --port 8765 \
  --root runtime/demo
```

打开 `http://127.0.0.1:8765`，或在桌面端填写该地址。使用 `runtime/demo/first-login.txt` 中的临时密码登录并修改密码。仅启动 `review_server.py` 即可在 CPU 上验证账号登录、管理后台、工作区路由与基础浏览。

## 启用完整计算功能

按 [完整部署指南](SELF_HOSTING.md) 准备独立人体骨架、机器人资产、重定向与动力学环境、Kimodo 与 MotionCorrection、授权模型以及可选视频动作提取环境，执行生成与编辑验收后同时启动 API 服务和独立队列：

```bash
# 终端 A：API 与下载监听器（配合反向代理时启用 --behind-proxy）
.retarget-venv/bin/python scripts/review_server.py --host 127.0.0.1 --port 18765 \
  --download-port 18767 --root runtime/farm --behind-proxy --origins https://kinora.example.com

# 终端 B：独立计算与调度队列
mkdir -p runtime/farm/tmp
SQLITE_TMPDIR="$PWD/runtime/farm/tmp" .retarget-venv/bin/python scripts/production_supervisor.py \
  --root runtime/farm --config data/farm/topics.json
```

运行 Kimodo 编辑与拼接验收（需在独立 `.kimodo-venv` 和 GPU 环境执行）：

```bash
.kimodo-venv/bin/python scripts/validate_kimodo_edit.py \
  --output runtime/edit-acceptance --gpu-root runtime/farm
```

检查通过后，通过自己的 HTTPS 反向代理（参考 `deploy/nginx.conf`）对外暴露服务，在桌面端填写团队 HTTPS 地址并使用自己的账号和 Gadget Key。当前全部功能代码已包含；没有预装的模型权重、硬件、私有机器人或外部 API 仍需部署者自行提供。

[发布验证](RELEASE_VALIDATION.md) 记录历史基准测试范围及限制。
