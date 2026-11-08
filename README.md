<div align="center">

# Kinora

**从动作灵感，到可审阅、可编辑、可导出的机器人动作。**

动作生成 · 动作库 · 机器人重定向 · 精修 · Agent 工具

[下载安装包](#下载) · [部署服务端](docs/SERVER.md) · [使用 Gadget](docs/GADGET.md) · [全部版本](https://github.com/PoirotJin/kinora/releases)

</div>

---

Kinora 将动作生成、检索、预览、重定向和精修放在同一个工作空间。使用桌面客户端连接团队服务器，也可以通过 Gadget 的命令行与 MCP 工具，把动作工作流接入已有的 Agent。

## 下载

**桌面端已编译完成，下载后即可安装，无需自行构建。** 安装后连接你的 Kinora 服务器并登录；生成与重定向计算由服务端承担。

| 下载 | 版本 | 适用环境 |
| --- | --- | --- |
| [macOS 安装包 · DMG](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/kinora_0.5.1_universal.dmg) | 0.5.1 | Apple 芯片与 Intel，Universal |
| [Windows 安装包 · EXE](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/kinora_0.5.1_x64-setup.exe) | 0.5.1 | x86_64 |
| [Linux 安装包 · DEB](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/kinora_0.5.1_amd64.deb) | 0.5.1 | Ubuntu 22.04+ / Debian 12+，x86_64 |
| [服务端源码 · ZIP（含 Gadget）](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/kinora-server-0.5.1.zip) | 0.5.1 | 完整服务端、计算队列，含 Gadget 0.15.2 |
| [Gadget · ZIP](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/kinora-gadget-0.15.2.zip) | 0.15.2 | Python 3.10+，CLI / MCP |

[SHA-256 校验文件](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/SHA256SUMS.txt) · [安装与更新说明](docs/INSTALL.md) · [版本与兼容性](docs/VERSIONS.md)

> 服务端源码已更新为配套 **0.5.0 完整版**，内含 **Gadget 0.15.2**。模型权重、GPU 环境与机器人资产需按部署指南安装；包内提供官方资产下载、真实生成/编辑验收及 `doctor --full` 环境检查。

## 选择你的开始方式

### 使用桌面端

1. 下载并安装适合系统的安装包。
2. 填写团队提供的 Kinora 服务器地址，使用自己的账号登录。
3. 在动作库检索、预览动作，再按服务器提供的能力生成、重定向或精修。

桌面 0.5.1 配合已启用相应能力的服务端，支持实体机器人网格预览、关节调整、关键帧草稿与原动作对比。候选经过检查并明确接受后才能作为已验收结果导出。

### 部署自己的服务器

下载服务端源码 ZIP，按 [服务端入门](docs/SERVER.md) 创建本机演示，再按 [完整部署指南](docs/SELF_HOSTING.md) 配置计算环境与团队 HTTPS 地址。

源码包含当前的 Kimodo 1–120 秒生成、局部修复、双片段拼接、NPZ/BVH 导出、自定义机器人、重定向、实体网格精修和动力学验收。账号页面和 Gadget 均随包提供。默认 CPU 演示可先验证登录、目录与预览，完整计算功能需完成环境配置。

本次已通过全新解压启动、156 项核心回归、49 项 Gadget 测试，以及真实 GPU 生成/修复/拼接与 G1 120 秒精修正反例验证。[查看验证范围](docs/RELEASE_VALIDATION.md)。 正式发行包已再次独立解压复验：156 项核心回归加 1 项首次启动检查、49 项 Gadget 测试，以及 GPU 与 G1 链路均通过；[查看复验摘要](docs/RELEASE_RECHECK_0.5.1.json)。

### 让 Agent 使用 Kinora

下载 Gadget，配置服务地址和自己的 Kinora API Key。它可以直接运行 CLI，也可以作为本地 MCP 子进程供 Codex / Cursor 调用。Gadget 不运行另一个大模型，不需要提供 Agent 客户端的登录凭据。

```bash
export MOTIONFARM_URL="https://your-kinora.example.com"
export MOTIONFARM_API_KEY_FILE="$HOME/.config/kinora/api-key.txt"
python3 motionfarm-gadget/motionfarm.py info
```

Key 文件由你自行保存；使用自己的服务器地址替换示例。完整设置见 [Gadget 入门](docs/GADGET.md)。

## 发布说明

这个仓库是 Kinora 的公开下载与文档入口。服务端源码在 Releases 的命名 ZIP 中；GitHub 自动生成的 “Source code” ZIP 仅包含此入口仓库。安装包与源码包的内容和版本见 [版本说明](docs/VERSIONS.md)。

源码包不包含模型权重、生产数据库、用户动作或账号凭据。

## 许可证

Kinora 自有代码（桌面客户端、服务端与 Gadget）以 [Apache License 2.0](LICENSE) 授权。第三方组件保留各自许可，不因此改为 Apache-2.0：holosoma 为 Apache-2.0，three.js 为 MIT，EB Garamond 字体为 SIL OFL 1.1。署名与说明见 [NOTICE](NOTICE)。另行下载或安装的模型权重、机器人资产与外部服务遵循各自条款。再分发时请保留 LICENSE、NOTICE 及包内第三方许可文件。

自动几何检查、动力学验证或下载完成，都不等于真机安全认证。请结合具体机器人、控制器与运行环境验证动作。
