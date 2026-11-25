> 在 Kinora 桌面客户端登录后进入「Gadget 接入」，即可下载随服务器提供的 Gadget 接入包（1.0.0-beta.3 服务端内含 Gadget **0.9.1**）、创建/撤销个人密钥和生成本机配置。普通用户无需登录管理员网页。

# Gadget 入门

- **随 1.0.0-beta.3 服务端提供的 Gadget（0.9.1）**：登录桌面客户端后从「Gadget 接入」下载 `gadget-kit.zip`，或使用服务端源码包 `kinora-server-1.0.0-beta.3.zip` 中的 `gadget/` 目录。0.9.1 支持 Kimodo `1–120` 秒时长校验（旧 HY-Motion-1.0 为 `1–12` 秒）、22 个 MCP 工具（含 `motion_task_*`）以及与服务端一致的 `goal`/`message` 4000 字符上限。
- **独立稳定包（0.15.2）**：[Gadget 0.15.2 下载](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/kinora-gadget-0.15.2.zip)（面向 0.5.2 稳定服务端，协议兼容，不含 1–120 秒扩展）。

Gadget 是 Python CLI 与 MCP 工具包，使用 Python 3.10+ 标准库。解压后得到 `motionfarm-gadget/`。完整工具说明、提示词指南和客户端配置模板都在包内。

1. 在 Kinora 桌面客户端的「Gadget 接入」中创建个人 API Key，保存到仅自己可读的文件。
2. 设置 `MOTIONFARM_URL` 和 `MOTIONFARM_API_KEY_FILE`，运行连接检查。
3. 根据 `info` 返回的能力选择操作；新编辑与动力学工具需要相应服务端。

```bash
export MOTIONFARM_URL="https://your-kinora.example.com"
export MOTIONFARM_API_KEY_FILE="$HOME/.config/kinora/api-key.txt"
export MOTIONFARM_OUTPUT_DIR="$HOME/kinora-output"
python3 motionfarm-gadget/motionfarm.py info
```

使用私有 CA 时通过 `MOTIONFARM_CA_FILE` 提供可信证书，保持 TLS 校验。

## MCP 接入

本地启动命令为 `python3 /绝对路径/motionfarm-gadget/motionfarm_mcp.py`。将包内 `codex-config.example.toml` 或 `cursor-mcp.example.json` 合并到现有客户端配置，替换其中的服务器地址、脚本绝对路径、密钥文件与输出目录。模板中的示例地址不是你的自托管实例地址。

Gadget 沿用 `motionfarm` 配置名及 `MOTIONFARM_*` 环境变量，以保持协议兼容。模型规划由你已有的 Agent 完成；不要把客户端订阅或登录凭据交给服务器。

可以让 Agent：

- 搜索并列出向左侧步的动作候选。
- 将指定动作重定向到明确指定的机器人。
- 按英文描述生成动作，并读取质量报告和下载产物。

生成、计算和语义检索可能消耗服务器配额。断线恢复保留原任务编号；不要因超时重复创建任务。候选接受与下载遵守服务端验收状态。
