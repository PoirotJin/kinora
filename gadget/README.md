# MotionFarm Gadget 0.9

让你已经登录、已经选好模型的 Codex / Cursor 调用 MotionFarm。Gadget 是工具接口，不运行另一个大模型，也不需要 OpenAI/Cursor 的 API Key 或订阅登录凭证。只需 Python 3.10+ 标准库和自己的 MotionFarm API Key。

## 配置

在 MotionFarm 网页 Gadget 中创建个人密钥。通过 `MOTIONFARM_API_KEY` 环境变量或 `MOTIONFARM_API_KEY_FILE` 指向本地仅本人可读的密钥文本文件提供凭证。不要把密钥放进对话、仓库或命令参数。

设置 `MOTIONFARM_URL=https://your-kinora.example.com:8765`，运行 `python3 motionfarm.py info` 测试连接。HTTPS 验证始终启用；私有 CA 可使用可信的 `MOTIONFARM_CA_FILE`。服务端不接收你的 Codex/Cursor 登录凭证。

## 接入 Codex / Cursor

MCP 使用本地 stdio 子进程，调用远端 HTTPS。解压到稳定目录后，修改附带模板中的绝对路径：

- Codex：将 `codex-config.example.toml` 的配置段合并到 `~/.codex/config.toml`，不要覆盖其他配置。
- Cursor：将 `cursor-mcp.example.json` 的 motionfarm 项合并到客户端 MCP 配置；也可从 Cursor 的 MCP 设置添加同样的本地命令。
- 两者都启动 `python3 /绝对路径/MotionFarm-Gadget/motionfarm_mcp.py`；URL、密钥文件、输出目录通过 env 提供。
- 在 Codex/Cursor 本身选择你账户可用的模型。模型负责规划任务；工具只执行指定操作。

模板不包含密钥，也不会自动修改客户端配置或创建账户。MCP 提供十四个独立工具：`get_prompt_guidance`、`check_motion_prompt`、`quality_report`、`list_tags`、`motionfarm_info`、`search_motions`、`get_motion`、`generate_motion`、`import_motion`、`retarget_motion`、`get_job`、`cancel_job`、`download_artifact`、`workflow_recovery`。

可以直接对本地 agent 说：

- “在 Gallery 找已通过审核的向左侧步动作，先列出候选，不生成。”
- “把动作 m00000001 重定向到机器人列表中的指定机器人。”
- “按这段英文生成 8 秒动作，跳过 Qwen 改写，只下载源动作。”
- “请用你当前的模型把这段中文整理成符合 MotionFarm 格式的英文，再生成并重定向；保留原文、实际提示词和结果。”

## CLI 原子操作

以下 ID 仅为格式示例。创建新任务请换新的稳定 ID；重试同一任务必须保留原 ID。

```bash
python3 motionfarm.py info
python3 motionfarm.py search --query 'sideways left' --status accepted
python3 motionfarm.py motion m00000001
python3 motionfarm.py generate --id example_request_001 --text 'A person walks slowly forward with relaxed arms and separate foot tracks throughout the motion.' --duration 8 --n 1 --prompt-mode exact_english
python3 motionfarm.py status generate example_request_001
python3 motionfarm.py download m00000001 --kind source --out ./source.npz
python3 motionfarm.py retarget --id 11111111111111111111111111111111 --motion m00000001 --robot unitree_g1_23dof
python3 motionfarm.py status retarget 11111111111111111111111111111111
python3 motionfarm.py download 11111111111111111111111111111111 --kind retarget --out ./reference.npz
python3 motionfarm.py cancel generate example_request_001
```

搜索是**所有关键词都匹配的英文描述检索**，不是语义向量搜索。不调用 Qwen/embedding；每页最多检查 500 条索引，返回最多 50 个动作。如果 `next_after` 非空，即使 items 为空也可用 `--after` 继续。只返回公共与本人、未删除且已生成的动作。中文需求可由本地 agent 先提炼成英文关键词。

## 提示词选项

Gadget 的生成请求仅支持 `exact_english`，CLI、MCP 和清单均默认使用此模式。**由当前本地 Codex/Cursor 模型先翻译或重写**，以 `A person ` 开头、12–70 个英文词，保留用户动作意图、左右方向、接触约束及动作安全策略。不擅自增加手腕旋转，不声称真机安全。已符合格式的文本可直接提交。`translate` 会明确拒绝；不会静默切回服务器 Qwen。网页任务仍可使用网页提供的服务端重写选项。

可选溯源字段：`original_description`（原始需求，1–2000 字符）和 `rewrite_agent`（`name`、`model`，各 1–120 字符）。它们是客户端自报信息，不作为计费或身份认证依据；不知道模型名时省略。CLI 对应 `--original-description`、`--rewrite-agent-name`、`--rewrite-agent-model`；MCP 使用同名 JSON 字段。服务器校验英文表面格式；不会验证提示词语义或据此认证动作安全。

**费用边界**：这条路径的服务器提示词重写费用为 0；服务器 embedding 仍可能产生 API 费用，生成计算与配额照常适用。Codex/Cursor 订阅或本地模型费用由用户在外部承担，MotionFarm 无法得知，不记为免费。生成状态中的 `description` 是提交的英文、`prompt` 是最终使用文本；原始需求用 `original_description` 单独保留。

## 可组合批处理

`python3 motionfarm.py run example.json --out ./results`

清单支持顶层或逐项 `workflow`：

- `generate`（默认）：只生成并下载 `source.npz`。
- `generate_retarget`：生成后重定向并下载 `reference.npz`；必须明确 `robot`。
- `retarget`：提供已有 `motion_id` 与明确的 `robot`，不重新生成。

`prompt_mode` 可放在顶层或逐项，但只能为 `exact_english`。`original_description` 放在逐项；`rewrite_agent` 可放在顶层或逐项。`duration` 为 1–120 的整数（Kimodo 链路上限 120 秒，旧 HY-Motion-1.0 链路为 12 秒）；n 为 1–10000；清单最多 1000 项。`workflow-state.json` 保存稳定 ID，失败/超时后用同一清单和输出目录恢复。修改清单需换目录。**0.2 的 run 默认只生成，与 0.1 默认总是重定向不同**；要保持原行为需明确 `workflow: "generate_retarget"`。

输出附带实际文本、请求文本、metadata、SHA256。失败会明确报告；不会自动换新 ID 重做。下载拒绝覆盖内容不同的文件。

## 上传格式与产物

`upload FILE --id 32位hex --text 描述` 支持最大 4 MiB、30 FPS、2–720 帧、HY 原生 SMPL-H 数值 NPZ：`fps`、`native_keypoints3d[T,52,3]`、`native_transl[T,3]`、`smpl_poses[T,156]` 或 `[T,52,3]`。单位米、Y-up，关节顺序必须与 HY 相符。BVH/FBX/其他模型需先明确转换；不接受 pickle。源动作下载使用相同四字段，不含 pickle/object 数组；旧动作若缺少这些字段会明确报错。

公共和本人生成动作可下载源 NPZ；本地上传仅本人可用，不进入公共 Gallery。下载冷归档动作无需手动回迁。重定向结果不代表人工验收或真机可运行。

## HTTP API

前缀 `/api/gadget/v1/`；Bearer MotionFarm key；写请求 JSON。现有 v1 路由保持兼容，新增：

| 请求 | 权限 | 用途 |
|---|---|---|
| GET info | read | 能力、机器人、主题 |
| GET search?q=&topic=&status=&after=0&limit=20 | read | 有界检索 |
| GET motion?id=动作ID | read | 安全的动作详情 |
| GET source?id=动作ID | read | 原始数值 NPZ |
| POST generate | generate | request_id,description,n,duration,prompt_mode（仅 exact_english）,original_description?,rewrite_agent? |
| GET generate?id=请求ID&after=0 | generate | 状态与分页结果 |
| POST cancel_generate | generate | id=请求ID |
| POST import | retarget | id,text,npz_base64 |
| POST retarget | retarget | id,motion_id,robot |
| GET retarget?id=作业ID | retarget | 状态 |
| POST cancel | retarget | id=重定向作业ID |
| GET download?id=作业ID | read | 重定向 NPZ |

轮询间隔至少10秒；每密钥120请求/分钟；按服务端消息处理额度不足与队列繁忙。MCP 不自动轮询或隐式启动后续任务。密钥不提供服务器 shell、用户管理、库删除或自动生产管理权限。

## 自检

`python3 -m unittest test_client.py -v`：小型本地协议与工作流测试，无真实 API / GPU 调用。

## v0.3：协作标签和标签组

Gallery 的每张动作卡片增加「标签 / 标注记录」。填写标签组和标签后添加自己的标注；首次使用的组和标签自动创建。组名和标签名各限 64 字符，去除首尾空格，统一全角字符，英文大小写不重复创建。词表由所有用户共享。

同一个动作上的相同标签可由多人分别标注，返回每个人的 `username` 和 `created_at`（Unix 秒）。登录身份由服务器决定，客户端不能指定标注人。只能撤回自己的标注；添加、撤回及重新添加都保留不可修改的历史记录。标签保存在审核元数据库中，不改动源动作文件，冷热归档切换不影响标签。

MCP 新增 `list_tags`（可按 `tag_group` 和 `after` 分页，`limit` 最大 200）。`search_motions` 新增三个可组合参数：

```json
{"tag_group":"动作类型","tag":"行走","tagged_by":"alice","limit":20}
```

三个字段按 AND 匹配同一条有效标注。只给 `tag` 会匹配任意组中同名标签；只给 `tag_group` 会匹配组内任意标签；`tagged_by` 精确匹配用户名。撤回后的标注不参与搜索。可同时结合 `query`、`topic` 和 `status`。按 `next_after` 翻页直到 null，包括有游标的空页。搜索和 `get_motion` 都返回带标注人和时间的 `tags`。词表是共享的，但 MCP 动作搜索仍只返回公共动作及调用者自己的动作。

```bash
python3 motionfarm.py tags --tag-group 动作类型
python3 motionfarm.py search --tag-group 动作类型 --tag 行走 --tagged-by alice
```

本版本 MCP 标签能力为只读，现有 `read` 权限即可使用。标注通过 Gallery 完成。页面的输入建议显示词表前 200 项，仍可直接输入其他组名和标签名；MCP 可以分页获取全部词表。

升级：下载 v0.9 客户端包，替换完整客户端文件（含指南和检查器），保留自己的 Key 文件、输出目录与 MCP 配置路径，然后重新连接 MCP。


## v0.4 quality, reproducibility and execution

`info` discovers model identities (checkpoint file stat, separately named config SHA256; no full-weight hash), robots, parameter schema and independent endpoints. Poll replies retain old fields and add a `task-v1` envelope with canonical status, terminal flag, error, result references and resume endpoint. Keep original request IDs across uncertain network errors; poll at least 10 seconds apart.

MCP `quality_report(kind,id)` returns per-check true/false/null and provenance. Null means unknown. Source screening and offline robot checks do not certify dynamics or hardware safety. Failed new retarget candidates are quarantined.

`retarget_motion` accepts either `parameters` or `preset_id` plus `preset_version`; an optional `parent_job_id` preserves an owned predecessor. Unknown parameters fail explicitly. Supported weights: smooth_weight (0–10), laplacian_weight (0.1–100), root_orientation_weight (1–200), limb_direction_weight (0.1–20). Safety and wrist policy are fixed.

`generate_motion` accepts an optional 32-bit `seed` and owned `parent_request_id`. A replay creates new IDs while preserving the effective model seed. Record the exact English prompt, checkpoint identity and runtime; replay recipes do not promise bitwise identity.

Search adds author, robot (your completed jobs), duration_min/max, quality and private collection ID. Unknown quality remains distinct from passed. Alias matching preserves original tag assignments and audit history.

CLI: `python motionfarm.py report source MOTION_ID` or `report retarget JOB_ID`. Manifest retarget items accept the same execution fields; each saved result includes quality.json.


### 可恢复执行示例

```bash
python3 motionfarm.py report source m00000001
python3 motionfarm.py search --author alice --duration-min 1 --duration-max 3 --quality incomplete
python3 motionfarm.py retarget --id 22222222222222222222222222222222 --motion m00000001 --robot unitree_g1_23dof --preset-id 33333333333333333333333333333333 --preset-version 2
```

示例 ID 需替换为自己的对象。网络超时重发相同 ID；已失败任务需要显式新 ID 和 parent_job_id（生成任务为 parent_request_id），旧结果不会被覆写。

## 本地提示词 Harness（v0.7）

无需依赖宿主自动读取 AGENTS.md：MCP 初始化 instructions 直接携带完整提示词指南；`get_prompt_guidance` 随时返回规则、示例、检查清单和版本/hash。指南来自随包 `PROMPT_GUIDE.md`，其中动作策略是项目 AGENTS.md 的版本化快照。

本地 agent 顺序：读取指南 → 保留用户意图重写 → `check_motion_prompt` → 自行完成语义清单 → `generate_motion`。检查器只校验格式和时长，不自动重写、不发网络请求、不调用 Qwen。通过检查不代表语义正确、无碰撞或适合真机。CLI 和清单提交也强制同样的格式检查。

```sh
python3 motionfarm.py prompt-guide
python3 motionfarm.py check-prompt --text 'A person walks slowly forward on separate foot tracks with relaxed arms clear of the torso.' --duration 8
```

这两个命令与两个新增 MCP 工具无需配置 API Key；CLI 检查失败退出码为 2。生成前仍需本地 agent 判断意图、接触冲突和时长可行性。更新时解压整个包，保留自己的配置与密钥，然后重新连接 MCP；只替换旧脚本会遗漏指南和检查器。


### v0.7：MCP 断线恢复

每次生成、重定向、导入都会在网络发送前，把稳定 ID 和参数 SHA256 写入
`MOTIONFARM_OUTPUT_DIR/.gadget-journal/`。记录按服务地址和凭据指纹隔离，
只保存恢复元数据，不保存密钥、原始提示词、上传文件或请求参数。
请保留原始请求参数，换凭据后旧记录不会自动合并。

重新连接后调用 `workflow_recovery` 分页读取记录（默认 20 条），也可传
`kind` 与 `id` 精确查询。`uncertain` 表示请求可能已经到达服务器：先调用
`get_job` 查询原 ID，确认后才能用原参数、原 ID 重试，不能换 ID 再生成。
同一个 ID 配上不同参数会在本地被拒绝。导入任务用 `get_motion` 查询。
恢复工具只读本地文件，不自动重试、不轮询、不追加生成。

任务完成后，按需调用 `download_artifact` 和 `quality_report`。下载路径、
SHA256、最近任务状态和是否读取过质量报告会持久保存。质量报告的未知项
必须明确报告为未验证，完成或下载成功不等于通过质量检查。
每个网络调用最多 3 次尝试、每次 socket 超时 20 秒，重试退避 5/10 秒；
服务端任务可能在客户端超时后继续执行。用户授权数量是生成预算上限，
质量失败不能触发未获授权的新生成。已有 13 个工具全部保留，新增恢复工具。


## v0.8：复用与新版本、可靠恢复

默认仍复用同动作、同机器人已有的成功版本，连同它原来的参数。仅传新的 `parameters`、`preset_version` 或 `parent_job_id` 不代表要求重新计算。查看返回的 `execution` / `result_version`，确认是复用还是新算。

用户明确要求参数试验或新算法版本时，MCP `retarget_motion` 添加 `force_new: true`；CLI 添加 `--force-new`；清单可在根或单个重定向 item 中设置 `"force_new": true`。这会消耗计算资源、使用选定参数并保留旧结果。使用新作业 ID；断线重试仍保持原 ID、原参数，不能把重新计算开关当作网络恢复手段。

```sh
python3 motionfarm.py retarget --id 22222222222222222222222222222222 --motion m00000001 --robot unitree_g1_23dof --parameters '{"smooth_weight":4}' --force-new
```

新清单的重定向 ID 包含 item ID、动作、机器人和执行参数，所以一个清单可以放同一动作的多个参数方案。旧 `workflow-state.json` 已保存的 retarget_id 会继续沿用，升级不新建重复任务。已出现同 ID 不同参数冲突的旧 state 会明确报错并保留原任务；请先检查服务器原作业，再将冲突项放入独立清单与输出目录。

导入日志现在在发出请求前保存 `import_id` 和 `server_motion_id`，并给出 `resume.tool` 与 `resume.arguments`。导入 ID 是 32 位十六进制；实际动作 ID 是 `mimp` + 导入 ID。恢复时使用 `get_motion(motion_id=server_motion_id)`，不要直接拿 import_id 查询动作。旧 v0.7 日志也会在本地推导该映射，不发请求、不重新上传。成功 ACK 的 motion_id 必须与该映射一致；不一致会保留 uncertain 状态等待检查。

修改 MotionFarm 密码后，旧 API Key 会失效，网页明确显示“密码已变更，此密钥已失效”，且不占用有效 Key 的 10 个名额。新凭据的本地日志仍与旧凭据隔离；不会自动将另一个账户的任务并入当前账户。


## v0.9：同账户换 Key 后恢复

MCP 首次提交前，客户端调用只读 `identity` 接口验证当前 Key。日志绑定服务器提供的稳定 `server_id/account_id`，密码或 Key 改变不改变该账户的日志目录；另一账户不会继承这些日志。文件里不保存原始 Key、密码、提示词或上传内容。

- **普通重连 / 断网查看**：`workflow_recovery({})` 默认只读本地文件，不联网。返回 `context.identity_source=previously_verified_local_cache` 只说明之前验证过，不代表 Key 目前未撤销或账户仍启用。所有服务器操作仍需实时通过鉴权。
- **更换 Key 后**：保持原 `MOTIONFARM_OUTPUT_DIR`，调用 `workflow_recovery({"verify_identity":true})`。新 Key 通过验证后，能读取同一账户已有记录；新 Key 尚未联网验证时，离线列表会明确要求验证，不读取其他账户目录。
- **升级前的 v0.8 记录**：若原 Key 仍可用，先用它执行 `verify_identity=true`；仅这个被验证凭证的旧目录会关联到账户，旧文件和 ID 保持不变。若旧 Key 已失效且从未关联，改用新 Key 做服务器发现：`workflow_recovery({"discover":true,"kind":"retarget","limit":20})`。也分别检查 `kind=generate` 和 `kind=source`，每类用返回的 `next_after` 继续。发现接口仅列出当前账号已受理的任务 / 上传 ID；即使 Key 属于管理员，也不会列出其他人的任务。
- **恢复动作**：使用记录中的 `resume`。生成/重定向查询原 ID；上传使用 `server_motion_id=mimp+import_id`。重新连接和发现不会重新提交、取消或生成任何动作。

`verify_identity` 是只读身份查询，任何当前有效的 Key 可查询自己的身份；`discover` 需要 Key 的 `read` 权限。撤销、过期、停用账户和密码失效 Key 都不能在线发现。服务器分页游标只用于同一 `kind` 的 `discover=true`；不要把它混用于默认本地文件列表。

服务器发现补回的是身份与状态，并不包含重试所需的原始参数。它无法发现未被服务器受理的请求，也不会猜测重提；若旧日志存在同一 ID 对应不同参数摘要，会明确报冲突，原文件保留。不要因为恢复列表为空而换新 ID 重跑。旧 CLI `run` 的 `workflow-state.json` 保持原兼容行为，换 Key 时保留该文件及输出目录即可。
