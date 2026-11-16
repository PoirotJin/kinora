# 发布 Kinora

1. 在 MotionFarm 的 `codex/kinora-cubic-090` 分支更新桌面版本。Desktop installers 会构建三平台安装包，沿用现有 Tauri 更新密钥签名，并对 Windows 和 Linux 执行安装及升级验证。成功后下载 `kinora-public-candidate-<version>` 的原始 Actions ZIP，保留工作流摘要中的 SHA-256。
2. 在公开 Kinora 仓库的 Actions 中，从 `main` 运行 **Publish desktop release**。指定新版本、原始候选 ZIP 的 SHA-256，以及它的 HTTPS 下载地址。也可配置仅对私有 MotionFarm 有 Actions 读取权限的 `KINORA_BUILD_READ_TOKEN`，用摘要中的 Actions artifact ID 直接读取。填写新的服务端或 Gadget 源码 ZIP 地址，或从指定的公开版本沿用。
3. 工作流会解包检查源码及其中的嵌套压缩包、各平台安装包、更新包和程序内压缩的界面资源。发现受保护 IP、Qwen/Gadget 明文凭据模式、无法读出的安装包或不匹配的更新签名会使任务失败。两个 IP 从当前已配置的 Actions Secret `KINORA_BLOCKED_IPS` 读取，缺少配置也会阻止发布。失败日志不会打印匹配项。勾选 `verify_only` 可以试跑同一套检查而不发布。
4. 检查通过后，发布任务上传完整附件和 SHA-256 清单，然后公开 Release。GitHub 提供同批签名文件和 `latest.json`。桌面客户端在首次使用时要求用户填写服务地址，已保存的地址继续使用。

在维护者的本机环境为 `KINORA_BLOCKED_IPS` 提供两个逗号分隔的地址后，通过 `python3 tools/package_server.py <prepared-source> <output.zip>` 调用同一个检查器。新源码 ZIP 也会在公开发布时独立复查。
