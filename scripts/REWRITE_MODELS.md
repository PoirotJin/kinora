# 提示词重写入口

网页定制任务支持选择服务器重写模型或英文原文直传。当前默认只有 Qwen qwen3.8-flash。

模型注册文件：动作数据根目录 `review/rewrite_models.json`。未创建时使用内置 Qwen 配置。新增服务时由管理员配置（客户端不能提供 URL 或密钥）：

```json
{"models":[{"id":"qwen-default","label":"Qwen · qwen3.8-flash","provider":"dashscope","model":"qwen3.8-flash","adapter":"qwen","key_file":"/home/jfc/.config/gen2humanoid/dashscope_api_key","enabled":true}]}
```

未来 OpenAI-compatible chat/completions 服务使用 `adapter=openai_compatible` 并增加 HTTPS `base_url`，密钥仍从服务器文件读取。接口必须支持 JSON object 输出和非流式响应；不支持此协议的模型需要单独适配，不可仅修改名称。价格配置参见 API_COSTS.md。

每个任务持久化 rewrite_model，成为幂等参数的一部分。失败后以新任务重试可重新选择模型。已完成动作及历史结果不改写。

Gadget v0.5 的生成入口默认且仅允许 exact_english：本地 agent 使用自己的模型先整理英文，再上传。显式 translate 被拒绝，不会静默改为服务器调用。要求以 A person 开头，12–70 个英文词。original_description 与 rewrite_agent（name/model）可选，记录为用户自报来源，服务器不声称核验本地模型身份。本地提示词仍接受长度、语言等校验，Embedding 仍可调用服务器 API；本地订阅费用不包含在服务器账本中。
