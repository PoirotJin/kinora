# 服务端入门

下载 [Kinora Server 0.5.2 完整源码](https://github.com/PoirotJin/kinora/releases/download/kinora-server-v0.5.2/kinora-server-0.5.2.zip)，解压后进入 `kinora-server-0.5.2/`。包内包含完整服务端、Gadget 0.15.2、依赖清单、资产安装脚本和验收工具。

## 先启动本机演示

使用 Linux 或 macOS，以及支持 scrypt 的 Python 3.10–3.12。完整 GPU 部署以 Linux 为准。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/seed_demo.py --root runtime/demo
.venv/bin/python scripts/review_auth.py admin --role admin \
  --root runtime/demo/review --credentials-file runtime/demo/first-login.txt
KINORA_DATA_DIR="$PWD/runtime/demo" bash commands/kinora-server.sh serve \
  --preview-assets "$PWD/runtime/demo/preview-assets"
```

打开 `http://127.0.0.1:8765`，或在桌面端填写该地址。使用临时密码文件登录并修改密码。目录演示只需 CPU；四关节合成动作不用于完整人体生成或重定向。

## 启用完整计算功能

按 [完整部署指南](SELF_HOSTING.md) 安装内置独立骨架与官方 G1 资产、重定向环境、Kimodo 与 MotionCorrection、授权模型，执行实际生成及编辑验收，再同时启动 API 与队列进程。

```bash
bash commands/kinora-server.sh doctor --full
```

检查通过后，连接团队 HTTPS 地址，使用自己的账号和 Gadget Key。当前全部功能代码已包含；没有预装的权重、硬件、私有机器人或外部 API 仍需运营者提供。

[发布验证](RELEASE_VALIDATION.md) 记录实跑范围及限制；旧服务端源码包已从公开下载中撤下，请使用当前版本。
