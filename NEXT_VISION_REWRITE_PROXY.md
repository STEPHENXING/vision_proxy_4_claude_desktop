# Next Step: Vision Rewrite Proxy

## Goal

Upgrade the current local proxy from a capture/route proxy into a vision rewrite proxy:

```text
Claude Desktop
-> CC Switch
-> http://127.0.0.1:9980/anthropic
-> Vision Rewrite Proxy
-> https://api.deepseek.com/anthropic
```

DeepSeek accepts Anthropic-compatible text/tool requests, but it does not understand image blocks. The proxy should translate image blocks into text descriptions before forwarding.

## Current Working Base

Use the existing proxy:

```text
vision_proxy/capture_proxy.py
vision_proxy/config.ccswitch-9980.capture.json
```

Current confirmed behavior:

```text
CLD/CC Switch sends Anthropic Messages format to 9980.
Image blocks arrive as base64 image blocks.
Multiple images arrive in order.
Historical images are resent in later conversation turns.
```

## CC Switch And CLD Background

`CLD` means Claude Desktop.

The practical route for this project is:

```text
CLD
-> CC Switch local routing/config layer
-> http://127.0.0.1:9980/anthropic
-> our proxy
-> https://api.deepseek.com/anthropic
```

DeepSeek already provides an Anthropic-compatible endpoint:

```text
https://api.deepseek.com/anthropic
```

So the proxy does not need to fully translate Anthropic protocol into OpenAI
protocol. It should mainly:

```text
accept Anthropic Messages requests
rewrite Claude model IDs to DeepSeek model IDs
rewrite image blocks into text descriptions
forward to DeepSeek's Anthropic-compatible endpoint
preserve streaming and tools
```

Recommended CC Switch provider setting:

```text
Request URL: http://127.0.0.1:9980/anthropic
API format: Anthropic Messages (native)
API key: DeepSeek API key
```

The proxy exposes Claude-style model IDs outward and maps them internally:

```text
claude-sonnet-* -> deepseek-v4-flash
claude-haiku-*  -> deepseek-v4-flash
claude-opus-*   -> deepseek-v4-pro
claude-fable-*  -> deepseek-v4-pro
```

This lets CLD/CC Switch keep using Claude role model IDs while the proxy sends
DeepSeek model IDs upstream.

## CLD Config Safety Notes

Prefer using CC Switch as the configuration UI. Do not directly edit CLD 3p
provider JSON unless absolutely necessary.

Important files observed on Windows:

```text
C:\Users\xing\AppData\Local\Claude\claude_desktop_config.json
C:\Users\xing\AppData\Local\Claude-3p\configLibrary\_meta.json
C:\Users\xing\AppData\Local\Claude-3p\configLibrary\<provider-id>.json
```

CC Switch manages the `Claude-3p/configLibrary` provider files and switches
`_meta.json` to the selected provider.

If manual editing is ever required:

```text
Always back up the file first.
Always write UTF-8 without BOM.
Validate JSON after writing.
Fully restart CLD after changes.
```

PowerShell 5 warning:

```text
Set-Content -Encoding UTF8 may write a UTF-8 BOM.
```

That can break CC Switch/CLD JSON parsing with errors like:

```text
expected value at line 1 column 1
Unexpected UTF-8 BOM
```

Use Python `json.dump(..., encoding="utf-8")` or .NET
`System.Text.UTF8Encoding($false)` for no-BOM writes.

Do not reintroduce old scripts that directly install/restore CLD providers.
The current clean approach is:

```text
CC Switch configures the provider URL.
vision_proxy only handles HTTP proxy/rewrite behavior.
```

## Required MVP Behavior

For each `POST /anthropic/v1/messages` request:

1. Parse JSON body.
2. Walk `messages[].content[]`.
3. Find blocks like:

```json
{
  "type": "image",
  "source": {
    "type": "base64",
    "media_type": "image/png",
    "data": "..."
  }
}
```

4. For each image block:
   - Compute cache key:

```text
sha256(media_type + ":" + base64_data)
```

   - If cache hit, reuse cached description.
   - If cache miss, call Qwen VL via ModelScope.

5. Replace image blocks with text blocks:

```json
{
  "type": "text",
  "text": "[Image 1 Description]\n..."
}
```

6. Preserve all non-image content exactly:
   - `system`
   - text blocks
   - tool definitions
   - `tool_choice`
   - `tool_use`
   - `tool_result`
   - streaming flag
   - beta query params

7. Forward rewritten body to DeepSeek Anthropic endpoint.
8. Keep SSE streaming passthrough unchanged.

## ModelScope Qwen VL Call

Endpoint:

```text
https://api-inference.modelscope.cn/v1/chat/completions
```

Model:

```text
Qwen/Qwen3-VL-235B-A22B-Instruct
```

Suggested prompt:

```text
Describe this image in detail. If it contains code, UI, error messages,
architecture diagrams, terminal output, or other technical content, describe
all visible text, structure, and visual relationships as precisely as possible.
Answer in Chinese.
```

Request shape should be OpenAI-compatible vision chat format:

```json
{
  "model": "Qwen/Qwen3-VL-235B-A22B-Instruct",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Describe this image in detail... Answer in Chinese."
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/png;base64,..."
          }
        }
      ]
    }
  ]
}
```

API key should come from environment:

```text
MODELSCOPE_API_KEY
```

Do not hard-code keys.

## Cache

Cache path:

```text
C:\Users\xing\.claude\vision_proxy_http\cache\image_descriptions.json
```

Cache entry:

```json
{
  "sha256:...": {
    "media_type": "image/png",
    "base64_len": 18400,
    "description": "...",
    "created_at": "2026-07-01T10:40:00",
    "model": "Qwen/Qwen3-VL-235B-A22B-Instruct"
  }
}
```

Why cache is required:

```text
Claude Desktop resends historical images in later turns.
Without cache, old images are repeatedly sent to Qwen VL.
That increases cost, latency, and failure probability.
```

## Failure Handling

If Qwen VL fails, do not fail the main request.

Insert:

```text
[Image Description unavailable: <short error>]
```

Then continue forwarding to DeepSeek.

## Timeout

Use a per-image timeout:

```text
30-45 seconds
```

Timeout should produce unavailable text, not abort the DeepSeek request.

## Size Guard

Initial MVP can use a simple base64 length guard:

```text
max_image_bytes: 8000000
```

If image is too large, insert:

```text
[Image Description unavailable: image too large]
```

Later improvement: add Pillow compression/resizing.

## Logging

Log to:

```text
C:\Users\xing\.claude\vision_proxy_http\proxy.log
```

Include:

```text
request_id
image_count
cache_hits
cache_misses
qwen_elapsed_ms
upstream_elapsed_ms
errors
```

Never log full API keys.

## Useful Output Files

Keep these capture outputs:

```text
latest-message-request.json
latest-message-summary.json
latest-request.json
latest-summary.json
```

Add rewritten-body visibility for debugging:

```text
latest-rewritten-message-request.json
```

This should contain the forwarded JSON after image-to-text rewrite.

## Acceptance Tests

1. Pure text request still works.
2. Single image question produces an answer based on image content.
3. Three images in one message are described in order.
4. Historical images hit cache on later turns.
5. Tool calls still preserve `tools`, `tool_choice`, `tool_use`, and `tool_result`.
6. Streaming responses still pass through.
7. Qwen VL failure still produces a DeepSeek answer containing the unavailable message.

## Recommended Implementation Order

1. Add config fields:

```json
{
  "vision_enabled": true,
  "vision_base_url": "https://api-inference.modelscope.cn/v1",
  "vision_model": "Qwen/Qwen3-VL-235B-A22B-Instruct",
  "vision_timeout_seconds": 45,
  "max_image_bytes": 8000000,
  "image_cache_path": "~/.claude/vision_proxy_http/cache/image_descriptions.json"
}
```

2. Add helpers:
   - `find_image_blocks`
   - `hash_image`
   - `load_image_cache`
   - `save_image_cache`
   - `describe_image_with_qwen`
   - `rewrite_images_to_text`

3. Rewrite body before `rewrite_model`.
4. Keep model mapping after vision rewrite.
5. Add focused tests using fake upstream and fake vision client.
