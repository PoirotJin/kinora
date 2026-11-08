# Gadget 入门

[Gadget 0.15.2 下载](https://github.com/PoirotJin/kinora/releases/download/kinora-v0.5.1/kinora-gadget-0.15.2.zip)

Gadget 是 Python CLI 与 MCP 工具包，使用 Python 3.10+ 标准库。解压后得到 `motionfarm-gadget/`。完整工具说明、提示词指南和客户端配置模板都在包内。

1. 从自己的 Kinora 实例获取个人 API Key，保存到仅自己可读的文件。
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

本地启动命令为 `python3 /绝对路径/motionfarm-gadget/motionfarm_mcp.py`。将包内 `codex-config.example.toml` 或 `cursor-mcp.example.json` 合并到现有客户端配置，替换其中的服务器地址、脚本绝对路径、密钥文件与输出目录。模板中的历史部署地址不是自托管实例地址。

Gadget 沿用 `motionfarm` 配置名及 `MOTIONFARM_*` 环境变量，以保持协议兼容。模型规划由你已有的 Agent 完成；不要把客户端订阅或登录凭据交给服务器。

可以让 Agent：

- 搜索并列出向左侧步的动作候选。
- 将指定动作重定向到明确指定的机器人。
- 按英文描述生成动作，并读取质量报告和下载产物。

生成、计算和语义检索可能消耗服务器配额。断线恢复保留原任务编号；不要因超时重复创建任务。候选接受与下载遵守服务端验收状态。

## 版本选择

本次独立包为 0.15.2，仅修正发布包自检中的陈旧版本断言并同步版本，能力与 0.15.0 相同。当前服务端源码 0.5.1 已包含相同的 Gadget 0.15.2，并包含对应的编辑、自定义机器人和动力学接口。完成服务端部署与验收后可使用这些工具；连接旧服务器时仍应先查询 `info`，不能假定新工具均可用。
