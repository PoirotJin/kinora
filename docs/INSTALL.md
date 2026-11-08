# 安装与更新

从 [下载表](../README.md#下载) 选择安装包，先与同一 Release 的 `SHA256SUMS.txt` 核对。

- **macOS**：打开 DMG，将 Kinora 拖入 Applications。当前为 ad-hoc 签名，尚未公证；首次可右键应用选择“打开”。若提示损坏或完整性异常，先核对下载文件，不要关闭系统安全机制。
- **Windows**：运行 x64 EXE 安装向导。当前没有 Windows 代码签名，可能出现 SmartScreen；仅在确认来源及校验值后选择继续。
- **Linux**：在下载目录执行 `sudo apt install ./kinora_0.5.1_amd64.deb`，由包管理器安装依赖。

校验命令（使用对应文件名）：

```bash
# macOS
shasum -a 256 kinora_0.5.1_universal.dmg
# Linux
sha256sum kinora_0.5.1_amd64.deb
```

```powershell
# Windows PowerShell
Get-FileHash .\kinora_0.5.1_x64-setup.exe -Algorithm SHA256
```

安装后填入服务器地址，再用该实例的账号登录。客户端不附带服务器账号、模型或本地 GPU 推理环境。使用自托管服务器时，功能以该服务器实际配置为准。

已有用户可在“设置 → 检查更新”查看更新。应用内更新取决于所连接服务的更新配置；本仓库发布安装包不改变既有更新服务。

验证范围：已有 macOS 真实更新验证，以及 Windows Server 2025、Ubuntu 22.04 / 24.04 的全新安装与覆盖升级检查。上述记录不等于对所有 Windows 桌面版本或全部交互功能的验收。
