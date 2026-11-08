# 完整服务端部署

以下命令在解压后的 `kinora-server-0.5.1/` 目录执行。推荐 Linux x86_64、Python 3.10，配有 NVIDIA GPU、驱动和足够磁盘空间。基础依赖采用固定版本；GPU 环境独立安装。模型与第三方资产需使用部署者自己的合法访问权限。

## 1. 基础服务与账号

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p runtime/farm
.venv/bin/python scripts/review_auth.py admin --role admin \
  --root runtime/farm/review --credentials-file runtime/farm/first-login.txt
```

临时密码文件权限为 `0600`。首次登录必须修改密码；其他成员使用相同命令但 `--role user` 创建。生产进程以独立普通系统用户运行，各团队使用独立目录和账号。

## 2. 人体骨架与 G1 模型资产

```bash
.venv/bin/python scripts/install_public_assets.py
```

人体使用随源码分发的 Kinora Human52 独立参数骨架，预览按骨骼与关节程序化绘制；无需下载人体模型、蒙皮或纹理。`--group rig` 可离线重新生成其描述文件。

脚本只从固定提交的 Unitree 官方仓库获取 29 个 G1 文件，逐个验证 `assets.lock.json` 中的 SHA-256。需要能够访问 `raw.githubusercontent.com`；离线部署可以复制已验证文件后重新运行校验。

CASBOT 资产需自行取得，或通过自定义机器人导入自己的模型。G1 安装不代表所有内置机器人均已配置。上游来源：[Unitree G1](https://github.com/unitreerobotics/unitree_ros/tree/master/robots/g1_description)，遵循其许可。

## 3. 重定向与动力学环境

```bash
python3 -m venv .retarget-venv
.retarget-venv/bin/python -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
.retarget-venv/bin/python -m pip install -r requirements-retarget.txt
.retarget-venv/bin/python scripts/retarget_mesh.py --model --robot unitree_g1_23dof
```

启动脚本在 `.retarget-venv` 存在时优先用它运行 API 和队列，保证机器人精修的子进程也有 MuJoCo 与求解依赖。全功能部署不要仅用基础 `.venv` 手动启动 API。

## 4. Kimodo 与 MotionCorrection

固定上游源码版本与本次验证一致：

```bash
mkdir -p .vendor
git clone --recurse-submodules https://github.com/nv-tlabs/kimodo.git .vendor/kimodo
git -C .vendor/kimodo checkout e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954
git -C .vendor/kimodo submodule update --init --recursive
python3 -m venv .kimodo-venv
.kimodo-venv/bin/python -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu124
.kimodo-venv/bin/python -m pip install -e .vendor/kimodo
.kimodo-venv/bin/python -m pip install .vendor/kimodo/MotionCorrection
.kimodo-venv/bin/python -m pip install numpy==1.26.4 scipy==1.15.3
.kimodo-venv/bin/python -c 'import torch, motion_correction; assert torch.cuda.is_available()'
```

MotionCorrection 需要 C++17 编译器与 CMake 3.15+（Ubuntu 可安装 `build-essential cmake`）。CUDA 12.4 的 PyTorch 需要匹配驱动；若使用其他 CUDA/驱动组合，需在自己的机器重新验收。参见 [Kimodo 官方安装说明](https://github.com/nv-tlabs/kimodo)。

## 5. 授权模型下载与真实预检

先在 Hugging Face 获得 `meta-llama/Meta-Llama-3-8B-Instruct` 的访问权限，再用自己的账号登录。

```bash
.venv/bin/python -m pip install huggingface_hub
.venv/bin/hf auth login
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/download_kimodo.py --download
.kimodo-venv/bin/python scripts/preflight_kimodo.py --run --gpu-root runtime/farm
```

下载包括 Kimodo SOMA RP v1.2、Llama 和两个 LLM2Vec 编码器适配器。实际生成预检通过后才写入本机 `ckpts/kimodo/ready.json`；不复制别人的就绪收据。脚本不会把缺少权重的实例标为可生成。

## 6. 修复与拼接验收

```bash
.kimodo-venv/bin/python scripts/validate_kimodo_edit.py \
  --output runtime/edit-acceptance --gpu-root runtime/farm
```

仅在上述命令成功退出且 `acceptance.json` 的 `passed` 为 `true` 时安装收据：

```bash
cp runtime/edit-acceptance/acceptance.json ckpts/kimodo/edit-ready.json
bash commands/kinora-server.sh doctor --full
```

验收实际运行生成、局部修复、直线拼接和转向拼接，检查选区外原件保持不变及数值边界。更换相关源码或资产后重新验收，不能手写 `passed=true`。

## 7. 启动 API 和独立队列

两个终端使用相同的绝对数据路径。先本机检查，生产服务用自己的 HTTPS 域名和反向代理。

```bash
# 两个终端都设置
export KINORA_DATA_DIR="$PWD/runtime/farm"

# 终端 A：先在本机运行
bash commands/kinora-server.sh serve

# 终端 B
bash commands/kinora-server.sh worker
```

对外 API 示例（TLS 在反向代理终止）：

```bash
bash commands/kinora-server.sh serve --host 127.0.0.1 --port 8765 \
  --behind-proxy --origins https://kinora.example.com
```

反向代理覆盖 `X-Real-IP` 并传递 Host，允许上传正文至少 20 MiB（长动作或模型可能更大，以接口限制为准）。不要将未加密 HTTP 监听直接暴露到公网。API 与队列均由服务管理器保持运行，使用同一环境变量与数据目录；启动队列不会自动授权大批量生成。

`curl http://127.0.0.1:8765/healthz` 仅确认 HTTP 服务运行；完整环境状态由 `doctor --full` 检查。桌面填写团队 HTTPS 地址。使用 G1 时在机器人列表明确选择 `unitree_g1_23dof`。

## 8. Gadget

同包 `gadget/` 和网页下载均为 0.15.2。修改初始密码后，由管理员签发个人 Key：

```bash
.retarget-venv/bin/python scripts/gadget_keys.py --root runtime/farm --owner admin create \
  --name 'My Gadget' --days 30 --key-file runtime/farm/gadget.key \
  --scope read --scope generate --scope retarget
```

将密钥安全交给对应成员，按 `gadget/README.md` 设置自己的 HTTPS 地址、密钥文件与输出目录。默认只读 Key 不允许提交计算。

## 可选服务

- **网页提示词重写**：将 `examples/rewrite_models.example.json` 复制为数据目录的 `review/rewrite_models.json`，配置自己的 HTTPS API、模型和密钥文件。英文 `exact_english` 路径不需要重写 API。
- **语义检索 / 自动提示词生产**：默认 DashScope 路径从服务用户的 `~/.config/gen2humanoid/dashscope_api_key` 读取密钥。配置自己的 Key 和费用预算；没有此服务时不能宣称中文语义检索已就绪。Gadget 的本地 Agent 重写不替代服务端 embedding。
- **邮箱注册与密码恢复**：在 API 服务的私有环境中配置 `MOTIONFARM_SMTP_HOST`、`MOTIONFARM_SMTP_PORT`、`MOTIONFARM_SMTP_USER`、`MOTIONFARM_SMTP_PASSWORD`、`MOTIONFARM_MAIL_FROM`、`MOTIONFARM_ACCOUNT_ORIGIN=https://kinora.example.com`。需要隐式 TLS 时设置 `MOTIONFARM_SMTP_SSL=1`。未配置 SMTP 时通过管理员创建账号。
- **应用内更新**：更新服务由运营者单独配置签名更新包与 `latest.json`。此源码包不包含签名私钥或预置更新载荷；用户始终可从公开 GitHub 下载已编译安装包。
- **旧包 OIDC 扩展**：0.4.18 管理工具版的 OIDC 扩展不是当前线上服务代码的一部分；本版账号入口使用当前的本地密码、邮箱与 Gadget Key，不将旧版 OIDC 功能冒充已验证能力。

源码包含全部当前功能路径；硬件、模型、私有机器人及外部服务仍由部署者提供。`doctor --full` 覆盖 G1 与 Kimodo 核心路径，不认证 SMTP、付费 API、所有 CASBOT 型号或真机安全。

## 可重复的机器人验收

公开资产和求解环境就绪后，可运行：

```bash
.retarget-venv/bin/python scripts/validate_g1_editor.py
```

使用包内明确标记的合成 G1 平衡姿态/手臂动作夹具，检查 3600 帧精修、全轨迹动力学、显式接受与下载、撤销/重做、权限，以及悬空反例拒绝。结果写入 `runtime/g1-acceptance/integration.json`，不会连接生产服务。此夹具不是动作生成或真机行走验收。

## 可选 TMR 自动语义评分

TMR 适配代码包含在 `g2h/tmr_scoring.py` 和 `scripts/auto_review_worker.py`。按 [官方 TMR 指南](https://github.com/Mathux/TMR) 获取运行时、HumanML3D 预训练模型和对应依赖，在独立环境安装 `transformers`。资产根目录需包含：

```text
TMR-master/src/model/actor.py
TMR-master/src/guofeats/
TMR-master/stats/humanml3d/guoh3dfeats/{mean.pt,std.pt}
models/tmr_humanml3d_guoh3dfeats/config.json
models/tmr_humanml3d_guoh3dfeats/last_weights/{motion_encoder.pt,text_encoder.pt}
distilbert/  # 完整的本地 tokenizer / config / model 文件
```

在该独立环境用 `python scripts/auto_review_worker.py --root runtime/farm --assets /absolute/path/to/tmr-assets --limit 16` 执行有界评分，再按需通过自己的服务定时器重复运行。模型、统计文件与 DistilBERT 必须相互兼容；本次没有重新验收 TMR 模型安装。该分数是语义相似性，不代表动力学或真机安全。
