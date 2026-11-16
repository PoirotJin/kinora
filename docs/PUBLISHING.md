# 发布 Kinora

1. 在 MotionFarm 的 `codex/kinora-cubic-090` 分支更新桌面版本。Desktop installers 会构建三平台安装包，沿用现有 Tauri 更新密钥签名，并对 Windows 和 Linux 执行安装及升级验证。成功后下载 `kinora-public-candidate-<version>` 的原始 Actions ZIP，保留工作流摘要中的 SHA-256。
2. 在公开 Kinora 仓库的 Actions 中，从 `main` 运行 **Publish desktop release**。指定新版本、原始候选 ZIP 的 SHA-256，以及它的 HTTPS 下载地址。也可配置仅对私有 MotionFarm 有 Actions 读取权限的 `KINORA_BUILD_READ_TOKEN`，用摘要中的 Actions artifact ID 直接读取。填写新的服务端或 Gadget 源码 ZIP 地址，或从指定的公开版本沿用。
3. 工作流会解包检查源码及其中的嵌套压缩包、各平台安装包、更新包和程序内压缩的界面资源。发现受保护 IP、Qwen/Gadget 明文凭据模式、无法读出的安装包或不匹配的更新签名会使任务失败。两个 IP 从当前已配置的 Actions Secret `KINORA_BLOCKED_IPS` 读取，缺少配置也会阻止发布。失败日志不会打印匹配项。勾选 `verify_only` 可以试跑同一套检查而不发布。
4. 检查通过后，发布任务会先上传到草稿，按 GitHub 返回的附件名称、大小和 SHA-256 复核整套文件，再公开 Release。仓库必须启用不可变发布。GitHub 提供同批签名文件和 `latest.json`。桌面客户端在首次使用时要求用户填写服务地址，已保存的地址继续使用。

在维护者的本机环境为 `KINORA_BLOCKED_IPS` 提供两个逗号分隔的地址后，通过 `python3 tools/package_server.py <prepared-source> <output.zip>` 调用同一个检查器。新源码 ZIP 也会在公开发布时独立复查。

## GitHub Releases 页面

正式发布从上述 Actions 入口执行。GitHub 的不可变发布会锁定今后公开的附件与标签。现有旧版不受此设置追溯保护，仓库所有者仍具备手动发布草稿的权限。修复已锁定附件时，用更高版本重新走发布流程。

### 公开后的自动核对

正式工作流会为通过门禁的附件生成签名记录，包含 Release ID、标签、目标提交和每个附件的大小及 SHA-256。记录私钥只在 `main` 的发布签名环境可用。此前核查过的七个版本使用仓库里的固定基线。

新 Release 公开或收到编辑事件时触发核对。每五分钟还会重新检查公开 Release，覆盖单独上传附件或事件延迟的情况。旧版出现额外或被替换的附件时仅撤下异常附件。没有可信记录的 Release 会被撤下。不可变版无法单独撤下异常附件，因此会撤下整版。缺少已核准的附件时会提醒维护者。GitHub 可能延迟定时运行，这项核对在公开后执行。

发现异常时，流程通过 GitHub Issue 指派并通知仓库所有者。Issue 仅记录 Release ID、文件数量和执行记录链接。可在 Actions 中手动选择 `dry_run` 运行只读核对。
