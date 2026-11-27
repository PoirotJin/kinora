# 第三方与资产

- Holosoma 部署适配源码保留在 `third_party/holosoma/`，包含其 Apache-2.0 LICENSE、NOTICE 与 THIRD_PARTY_LICENSES。这里是带 Kinora 适配的部署快照，不是原版上游。
- 网页 three.js 保留 `web/review/THREE-LICENSE.txt`；账号页字体保留 `web/account/fonts/` 内许可。
- `assets.lock.json` 记录从官方固定提交下载的 Unitree G1 网格。文件由部署者从上游下载；请遵守上游条款。源码 ZIP 不包含这些外部网格资产。人体骨架由 `g2h/human_rig.py` 独立定义，随源码分发，可离线重建。
- Kimodo、MotionCorrection、Meta Llama 和 LLM2Vec 按各自上游许可和访问要求获取。模型权重不随此包发布。
- Humanoid 机器人资产和其他私有模型须由具备授权的部署者提供。合成演示数据由 `scripts/seed_demo.py` 创建，不包含生产用户动作。

Kinora 自有代码以 Apache License 2.0 授权，见 LICENSE 和 NOTICE。
