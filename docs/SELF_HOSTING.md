# 完整服务端部署

以下命令在解压后的 `kinora-server-1.0.0-beta.3/` 目录执行。推荐 Linux x86_64、Python 3.10–3.12，配有 NVIDIA GPU、驱动和足够磁盘空间。基础依赖与 GPU 环境独立安装；模型与第三方资产需使用部署者自己的合法访问权限。

## 1. 基础服务与账号

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p runtime/farm/review runtime/farm/tmp
.venv/bin/python scripts/review_auth.py admin --role admin \
  --root runtime/farm/review --credentials-file runtime/farm/first-login.txt
```

临时密码文件权限为 `0600`。首次登录必须修改密码；其他成员使用相同命令但 `--role user` 创建。生产进程以独立普通系统用户运行，各团队使用独立目录和账号。

## 2. 人体骨架与机器人模型资产

人体使用随源码分发的 Kinora Human52 独立参数骨架（`data/human_rig/j_template.bin`、`data/human_rig/kintree.bin`），预览按骨骼与关节程序化绘制，无需下载外部人体模型、蒙皮或纹理。

机器人资产（如 Unitree G1）与私有机器人模型需由部署者合法取得后放入对应的 `third_party/holosoma/...` 模型目录或通过桌面端「自定义机器人」导入。生成的机器人网格与预览文件存放在 `web/review/robot/` 下供客户端加载。

## 3. 重定向与动力学环境

使用单独的 `.retarget-venv` 虚拟环境安装 PyTorch（CPU）、MuJoCo 及动力学求解依赖，并可生成机器人预览网格：

```bash
python3 -m venv .retarget-venv
.retarget-venv/bin/python -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
.retarget-venv/bin/python -m pip install -r third_party/holosoma/requirements.txt
.retarget-venv/bin/python scripts/retarget_mesh.py --model --robot unitree_g1_23dof
```

建议直接用 `.retarget-venv/bin/python` 启动 `scripts/review_server.py` 与 `scripts/production_supervisor.py`，保证机器人精修与求解子进程均可找到 MuJoCo 与求解依赖。全功能部署不要仅用基础 `.venv` 启动队列。

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

## 5. 授权模型下载与生成配置

先在 Hugging Face 获得 `meta-llama/Meta-Llama-3-8B-Instruct` 的访问权限，再用自己的账号登录并拉取 Kimodo SOMA RP v1.2 及编码器权重到实例的 `ckpts/` 目录。

设置环境变量 `MOTIONFARM_GENERATOR_MODEL=Kimodo-SOMA-RP-v1.2`（默认使用 `HY-Motion-1.0`）以启用 Kimodo 1–120 秒生成、场景生成和编辑链路，仅在完成模型安装与本机核验后设置 `ckpts/kimodo/ready.json`。不要复制他人的就绪收据，也不要把缺少权重的实例标为可生成。

## 6. 修复与拼接验收

```bash
.kimodo-venv/bin/python scripts/validate_kimodo_edit.py \
  --output runtime/edit-acceptance --gpu-root runtime/farm
```

仅在上述命令成功退出且 `acceptance.json` 的 `passed` 为 `true` 时安装收据：

```bash
cp runtime/edit-acceptance/acceptance.json ckpts/kimodo/edit-ready.json
```

验收实际运行生成、局部修复、直线拼接和转向拼接，检查选区外原件保持不变及数值边界。更换相关源码或资产后重新验收，不能手写 `passed=true`。

## 7. 视频动作提取环境（可选）

视频动作提取（`/api/harness/video-motion`）依赖独立的 MediaPipe Python 环境与 Pose Landmarker 模型，由 `scripts/video_motion.py` 按以下路径查找：

- Python 解释器：环境变量 `MOTIONFARM_VIDEO_PYTHON`（默认 `.video-venv/bin/python`）
- 模型权重文件：环境变量 `MOTIONFARM_VIDEO_MODEL`（默认 `ckpts/video/pose_landmarker_heavy.task`）

同时需启用 Kimodo 场景链路（`MOTIONFARM_GENERATOR_MODEL=Kimodo-SOMA-RP-v1.2` 且 Kimodo 编辑与生成就绪）。若缺少上述运行时或模型文件，接口会返回“服务端未启用视频动作提取”。

## 8. 启动 API 与独立计算队列

两个进程使用同一环境变量与绝对数据目录路径。先本机检查，生产服务用自己的 HTTPS 域名和反向代理。

```bash
# 终端 A：API 与独立下载端口（反向代理部署）
.retarget-venv/bin/python scripts/review_server.py --host 127.0.0.1 --port 18765 \
  --download-port 18767 --root "$PWD/runtime/farm" \
  --behind-proxy --origins https://kinora.example.com

# 终端 B：独立计算与调度队列
SQLITE_TMPDIR="$PWD/runtime/farm/tmp" .retarget-venv/bin/python scripts/production_supervisor.py \
  --root "$PWD/runtime/farm" --config "$PWD/data/farm/topics.json"
```

反向代理模板见 `deploy/nginx.conf`：需覆盖 `X-Real-IP` 并传递 `Host`，为视频与自定义机器人上传允许最大 45 MiB 正文，并将全部受保护下载路由代理至 `--download-port`（`18767`）且关闭 `proxy_buffering`。不要将未加密 HTTP 监听直接暴露到公网。API 与队列均由服务管理器保持运行（定时任务与后台服务模板见 `deployment/` 和 `deploy/`）；启动队列不会自动授权大批量生成。

使用 G1 时在机器人列表明确选择 `unitree_g1_23dof`。

## 9. Gadget

服务端源码 `gadget/` 与桌面端下载的 `gadget-kit.zip` 在 `1.0.0-beta.3` 中均为 Gadget `0.9.1`。修改初始密码后，由管理员或用户本人签发个人 Key：

```bash
.retarget-venv/bin/python scripts/gadget_keys.py --root runtime/farm --owner admin create \
  --name 'My Gadget' --days 30 --key-file runtime/farm/gadget.key \
  --scope read --scope generate --scope retarget
```

将密钥安全交给对应成员，按 `gadget/README.md` 设置自己的 HTTPS 地址、密钥文件与输出目录。默认只读 Key 不允许提交计算。

## 可选服务

- **网页提示词重写**：将重写配置放到数据目录的 `review/rewrite_models.json`，配置自己的 HTTPS API、模型和密钥文件。英文 `exact_english` 路径不需要重写 API。
- **语义检索 / 自动提示词生产**：默认 DashScope 路径从服务用户的 `~/.config/gen2humanoid/dashscope_api_key` 读取密钥。配置自己的 Key 和费用预算；没有此服务时不能宣称中文语义检索已就绪。Gadget 的本地 Agent 重写不替代服务端 embedding。
- **邮箱注册与密码恢复**：在 API 服务的私有环境中配置 `MOTIONFARM_SMTP_HOST`、`MOTIONFARM_SMTP_PORT`、`MOTIONFARM_SMTP_USER`、`MOTIONFARM_SMTP_PASSWORD`、`MOTIONFARM_MAIL_FROM`、`MOTIONFARM_ACCOUNT_ORIGIN=https://kinora.example.com`。需要隐式 TLS 时设置 `MOTIONFARM_SMTP_SSL=1`。未配置 SMTP 时通过管理员创建账号。
- **应用内更新**：更新服务由运营者单独配置签名更新包与 `latest.json`。此源码包不包含签名私钥或预置更新载荷；用户始终可从公开 GitHub 下载已编译安装包。

源码包含全部当前功能路径；硬件、模型、私有机器人及外部服务仍由部署者提供。

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

在该独立环境用 `python scripts/auto_review_worker.py --root runtime/farm --assets /absolute/path/to/tmr-assets --limit 16` 执行有界评分，再按需通过自己的服务定时器重复运行（参考 `deployment/motionfarm-auto-review.service` 与 `.timer`）。模型、统计文件与 DistilBERT 必须相互兼容；本次没有重新验收 TMR 模型安装。该分数是语义相似性，不代表动力学或真机安全。
