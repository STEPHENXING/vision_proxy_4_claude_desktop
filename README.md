# Claude Desktop Vision Proxy

这是一个本地 Anthropic-compatible 代理，用来给 Claude Desktop 接入
DeepSeek，并通过视觉模型把图片转成文本描述，让 DeepSeek 间接“看见”图片。

当前推荐链路：

```text
Claude Desktop 3p Provider
-> http://127.0.0.1:9980/anthropic
-> Vision Proxy
-> https://api.deepseek.com/anthropic
```

以前可以用 CC Switch 管理 3p provider；现在 proxy 自带管理页，可以直接修改
Claude Desktop 3p 配置，所以日常使用不再需要 CC Switch。CC Switch 仍然可以保留
作为备用配置 UI。

proxy 会做这些事：

```text
接收 Claude Desktop 的 Anthropic Messages 请求
把 image block 调用视觉模型改写成 text block
把 Claude 模型 ID 映射成 DeepSeek 模型 ID
保留 tools / tool_choice / tool_use / tool_result / streaming
转发到 DeepSeek Anthropic-compatible endpoint
```

## 启动

硅基流动：

```powershell
cd C:\Users\xing\Documents\antigravity-proj\boson-ai\vision_proxy
$env:GUIJILIUDONG_API_KEY="你的硅基流动 token"
python .\capture_proxy.py --config .\config.ccswitch-9980.capture.json
```

ModelScope：

```powershell
$env:MODELSCOPE_API_KEY="你的 ModelScope token"
```

健康检查：

```powershell
curl http://127.0.0.1:9980/health
```

管理页面：

```text
http://127.0.0.1:9980/admin
```

## 管理页面

管理页面可以做这些事：

```text
切换视觉 provider：siliconflow / modelscope
切换视觉模型：paddleocr / qwen3-vl-8b / qwen3-vl-32b / ModelScope 235B
开启或关闭图片改写
修改视觉模型超时和图片大小上限
查看视觉 API key 是否已设置
查看和清理图片描述缓存
查看最近请求 summary 和 proxy log
应用 Claude Desktop 3p gateway 配置
从 proxy 自动创建的备份恢复 Claude Desktop 3p 配置
```

应用或恢复 Claude Desktop 3p 配置后，需要重启 Claude Desktop 才会生效。

## Claude Desktop 3p 配置

管理页面会写入 Claude Desktop 3p 配置目录：

```text
C:\Users\xing\AppData\Local\Claude-3p\configLibrary
```

它只会操作当前配置里的 provider 文件和 `_meta.json`。应用 3p gateway 时，
只有检测到配置确实会变化才会创建备份；如果当前已经是
`http://127.0.0.1:9980/anthropic` 且 API key 没有变化，重复点击 Apply 不会新增备份。
备份目录：

```text
C:\Users\xing\AppData\Local\Claude-3p\configLibrary\vision-proxy-backups
```

推荐 provider URL：

```text
http://127.0.0.1:9980/anthropic
```

Claude Desktop 会继续拼接 Anthropic API 路径，例如：

```text
http://127.0.0.1:9980/anthropic/v1/messages
```

proxy 会转发到：

```text
https://api.deepseek.com/anthropic/v1/messages
```

## CC Switch

现在不再必须使用 CC Switch。

如果你仍想用 CC Switch 管理 provider，可以这样配置：

```text
Request URL: http://127.0.0.1:9980/anthropic
API format: Anthropic Messages (native)
API key: DeepSeek API key
```

但如果已经在管理页里点过 `Apply 3p Gateway`，Claude Desktop 可以直接走本
proxy，不需要再通过 CC Switch 中转。

## 视觉模型

配置文件：

```text
C:\Users\xing\Documents\antigravity-proj\boson-ai\vision_proxy\config.ccswitch-9980.capture.json
```

核心字段：

```json
{
  "vision_provider": "siliconflow",
  "vision_model": "qwen3-vl-8b"
}
```

硅基流动使用环境变量：

```text
GUIJILIUDONG_API_KEY
```

ModelScope 使用环境变量：

```text
MODELSCOPE_API_KEY
```

硅基流动模型别名：

```text
paddleocr    -> PaddlePaddle/PaddleOCR-VL-1.5
qwen3-vl-8b  -> Qwen/Qwen3-VL-8B-Instruct
qwen3-vl-32b -> Qwen/Qwen3-VL-32B-Instruct
```

如果视觉 provider 不可用，或者对应 API key 没有设置，proxy 不会中断主请求。
它会插入一段不可用提示文本，然后继续转发给 DeepSeek。

## 缓存和调试文件

图片描述缓存：

```text
C:\Users\xing\.claude\vision_proxy_http\cache\image_descriptions.json
```

最近一次原始请求：

```text
C:\Users\xing\.claude\vision_proxy_http\dumps\latest-message-request.json
```

最近一次摘要：

```text
C:\Users\xing\.claude\vision_proxy_http\dumps\latest-message-summary.json
```

最近一次转发给 DeepSeek 的改写后 JSON：

```text
C:\Users\xing\.claude\vision_proxy_http\dumps\latest-rewritten-message-request.json
```

确认图片改写是否发生，可以看 summary：

```text
image_block_count > 0
```

然后看 rewritten JSON 里是否已经没有 `type: "image"`，并出现
`[Image 1 Description]` 文本块。
