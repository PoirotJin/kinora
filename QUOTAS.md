# 权限撤销与资源配额

禁用账户会撤销 session，并立即取消等待作业。正在执行的定制 GPU 小批次保留取消标记，完成当前批次的持久化后停止；重定向进程可安全终止。工作进程在 claim、API 请求前和后续批次边界重新读取账户状态；账户数据库不可读时不批准新工作。

普通用户按执行的小批次预留额度，因此已有的大 N 请求不会因为整单预留而直接失败。额度暂不足时保留队列，每 60 秒重新检查，并在作业 message 中解释原因。管理员不受用户额度限制，仍受全局磁盘保护和任务执行超时限制。

环境变量（均为普通用户默认上限）：

- `MOTIONFARM_USER_MOTION_SECONDS=120000`：近 24 小时生成动作总时长，单位秒。
- `MOTIONFARM_USER_GPU_SECONDS=21600`：近 24 小时 GPU 批次进程占用时间，单位秒，包含加载与保存，不代表 CUDA kernel 的精确运行时间。
- `MOTIONFARM_USER_RETARGET_SECONDS=21600`：近 24 小时重定向进程占用时间，单位秒。
- `MOTIONFARM_USER_STORAGE_BYTES=21474836480`：部署本功能后累计产生的结果数据，20 GiB；删除或归档不返还该额度。它不是磁盘当前占用量，热层与冷层各 800 GB 的容量账本分别管理；详见 docs/STORAGE_TIERS.md。
- `MOTIONFARM_USER_API_TOKENS_5H=80000`、`MOTIONFARM_USER_API_TOKENS_WEEK=400000`：5 小时和 7 天滚动 API Token 上限。

`review/resource_quota.sqlite3` 保存资源预留与结算；`review/api_quota.sqlite3` 保存 API 额度，独立于可失效的 `api_usage.sqlite3` 统计写入。账户审计保存在 `accounts.sqlite3.security_events`，包含操作、操作者、目标账户、来源和结果，不包含密码、session、API Key 或用户提示词。

API 在网络调用前原子预留 UTF-8 输入字节数、输出 Token 上限及固定格式余量；它是保守估计，不是计费 Token。返回 usage 后按真实值结算（含合法的 0 Token）。超时、进程退出或结算暂不可用时，未确认调用继续占用预留，避免自动返还导致重复消耗；之后依照滚动窗口自然到期。真实 usage 若高于预留仍记录真实值，阻止后续调用，不能宣称对任何未来模型计费协议提供数学上的零超额保证。

旧 `api_usage.sqlite3.spend` 继续计入 API 限额。GPU/动作时长/结果字节配额从部署开始计量，不扫描或追补旧产物。SQLite 预留事务串行化并发提交，拒绝时不写入部分资源项。

验证：`test_quota_revocation.py` 覆盖并发额度竞争、拒绝前不发 API 请求、统计库失败不丢账、未知调用保留预留、结算幂等、账户停用与 stale claim、队列等待和审计脱敏；现有 custom/retarget/access 测试使用与生产一致的账户目录。
