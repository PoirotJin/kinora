# 安装与更新

从 [下载表](../README.md#downloads) 选择对应安装包，先与同一 Release 的 `SHA256SUMS.txt` 核对。

## 1.0.0-beta.3 预发布安装包

- **macOS 测试版**：下载 `kinora_1.0.0-beta.3_universal.dmg`，打开 DMG 后将 Kinora 拖入 Applications。支持 macOS 11+ 的 Apple silicon 与 Intel Mac；当前为 ad-hoc 签名，尚未公证，首次可右键应用选择“打开”。
- **Ubuntu 测试版**：在下载目录执行 `sudo apt install ./kinora_1.0.0-beta.3_amd64.deb`（Ubuntu 22.04+ x86_64）。

校验命令（1.0.0-beta.3）：

```bash
# macOS
shasum -a 256 kinora_1.0.0-beta.3_universal.dmg
# Linux
sha256sum kinora_1.0.0-beta.3_amd64.deb
```

预发布版独立于稳定版更新通道（`latest.json`）；如需升级测试版，请直接从对应 Release 下载新安装包覆盖安装。

## 稳定版安装包（0.7.0）

- **macOS**：打开 DMG，将 Kinora 拖入 Applications。当前为 ad-hoc 签名，尚未公证；首次可右键应用选择“打开”。若提示损坏或完整性异常，先核对下载文件，不要关闭系统安全机制。
- **Windows**：运行 `kinora_0.7.0_x64-setup.exe` 安装向导。当前没有 Windows 代码签名，可能出现 SmartScreen；仅在确认来源及校验值后选择继续。
- **Linux**：在下载目录执行 `sudo apt install ./kinora_0.7.0_amd64.deb`，由包管理器安装依赖。

校验命令（稳定版 0.7.0）：

```bash
# macOS
shasum -a 256 kinora_0.7.0_universal.dmg
# Linux
sha256sum kinora_0.7.0_amd64.deb
```

```powershell
# Windows PowerShell
Get-FileHash .\kinora_0.7.0_x64-setup.exe -Algorithm SHA256
```

## 连接、账号与 Gadget 接入

安装后填入团队 Kinora 服务器地址，再用该实例的账号登录。客户端不附带服务器账号、模型或本地 GPU 推理环境。使用自托管服务器时，功能以该服务器实际配置为准。

- **账号入口**：点击“申请账号”或“找回密码”会自动打开系统默认浏览器访问服务端的 `/account` 页面。申请页提供提交申请与进度查询；管理员审批通过后，使用申请时设置的用户名和密码登录。
- **应用内更新**：稳定版可在“设置 → 检查更新”查看签名更新。其他自托管实例需由各自管理员配置更新服务。
- **Gadget 接入**：普通用户登录客户端并进入主导航「Gadget 接入」，即可下载随服务端提供的 Gadget 包、创建个人 API Key 并生成本机配置文件，无需登录管理员网页。密钥默认只读，按需勾选生成或重定向权限。
