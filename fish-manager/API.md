# fish-manager API 调用文档

本文档描述 fish-manager 对外 HTTP API 的调用方式。示例默认 manager 地址为 `http://127.0.0.1:8080`。

## 通用约定

### 鉴权

除 `GET /health` 外，公开 API 都需要 OpenAI 兼容 Bearer Token：

```http
Authorization: Bearer <OPENAI_API_KEY>
```

Token 来自 manager 环境变量 `OPENAI_API_KEYS`，多个 key 使用英文逗号分隔。未标注等级的 token 默认最高支持 `v3`；也可以写成 `sk-live-1:v1,sk-live-2:v3`。`v1` token 可以调用 `v1` 到 `v4`，`v3` token 只能调用 `v3` 和 `v4`。

### 请求头

JSON 请求使用：

```http
Content-Type: application/json
```

语音合成接口可以指定目标 worker：

```http
X-Fish-Worker-ID: worker-a
```

`X-Worker-ID` 也可作为兼容别名。指定 worker 后，本次请求只会发给该 worker；如果该 worker 失败、过载或断开，manager 不会 fallback 到其他 worker。

语音合成接口可以通过 header 指定服务等级：

```http
X-Fish-Priority: v3
```

支持 `v1`、`v2`、`v3`、`v4` 四个等级，`v1` 最高。未指定时默认为 `v3`。

### 错误响应

错误响应统一为 JSON：

```json
{
  "error": {
    "message": "error message",
    "type": "invalid_request_error",
    "code": "bad_request"
  }
}
```

常见状态码：

- `400`: 请求字段错误，`code=bad_request`
- `401`: 鉴权失败，`code=invalid_api_key`
- `404`: 资源不存在，`code=not_found`
- `429`: 没有可用 worker 或指定 worker 不可用，`code=tts_overloaded`，响应头包含 `Retry-After: 2`
- `502`: worker 或上游推理失败，`code=upstream_error`
- `500`: manager 内部错误，`code=internal_error`

## 接口总览

| Method | Path | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/health` | 否 | manager 健康检查 |
| `GET` | `/v1/workers` | 是 | 查看已连接 worker |
| `POST` | `/v1/audio/speech` | 是 | OpenAI 风格语音合成 |
| `POST` | `/v1/tts` | 是 | Fish Speech 兼容语音合成 |
| `POST` | `/v1/references/add` | 是 | 添加参考音色 |
| `GET` | `/v1/references/list` | 是 | 列出参考音色 ID |
| `POST` | `/v1/references/update` | 是 | 重命名参考音色 ID |
| `DELETE` / `POST` | `/v1/references/delete` | 是 | 删除参考音色 |

## `GET /health`

健康检查。

```bash
curl http://127.0.0.1:8080/health
```

成功响应：

```json
{
  "status": "ok"
}
```

## `GET /v1/workers`

查看当前连接到 manager 的 worker 状态。

```bash
curl http://127.0.0.1:8080/v1/workers \
  -H 'Authorization: Bearer <OPENAI_API_KEY>'
```

成功响应：

```json
{
  "data": [
    {
      "worker_id": "worker-a",
      "version": "0.1.0",
      "model_id": "fishaudio/fish-speech-s2pro",
      "model_revision": null,
      "gpu_name": "NVIDIA GeForce RTX 4090",
      "gpu_count": 1,
      "vram_total_mb": 24564,
      "max_running_requests": 4,
      "max_queued_requests": 2,
      "worker_max_inflight": 4,
      "worker_max_queue": 2,
      "sglang_url": "http://127.0.0.1:30000",
      "ready": true,
      "sglang_healthy": true,
      "inflight": 0,
      "queued": 0,
      "vram_used_mb": 12000,
      "vram_free_mb": 12000,
      "gpu_utilization_percent": 10.0,
      "ewma_latency_ms": 850.0,
      "last_error": null,
      "connected_at": "2026-06-22T08:00:00Z",
      "last_heartbeat_at": "2026-06-22T08:00:05Z",
      "manager_inflight": 0
    }
  ]
}
```

## `POST /v1/audio/speech`

OpenAI 风格的语音合成接口。请求体中的 manager 专用字段会在转发给 worker 前移除：

- `voice` / `voice_id`
- `references`

其他字段会原样透传给 worker，例如 `temperature`、`top_p`、`top_k`、`repetition_penalty`、`max_new_tokens`、`seed` 等。

### 使用已保存参考音色

```bash
curl -X POST http://127.0.0.1:8080/v1/audio/speech \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -o output.wav \
  -d '{
    "input": "Hello from fish-manager",
    "voice": "speaker_a",
    "response_format": "wav",
    "stream": false,
    "speed": 1.25,
    "temperature": 0.8,
    "top_p": 0.8,
    "max_new_tokens": 1024
  }'
```

### 使用内联参考音频

```bash
curl -X POST http://127.0.0.1:8080/v1/audio/speech \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -o output.wav \
  -d '{
    "input": "Hello from inline reference",
    "response_format": "wav",
    "stream": false,
    "references": [
      {
        "text": "reference transcript",
        "content_type": "audio/wav",
        "audio_base64": "UklGRiQAAABXQVZFZm10IBAAAAABAAEA..."
      }
    ]
  }'
```

`references[].audio` 也可以作为 `audio_base64` 的别名。manager 不接受 `references[].audio_path`，需要调用方直接传 base64 音频。

### 指定 worker

```bash
curl -X POST http://127.0.0.1:8080/v1/audio/speech \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -H 'X-Fish-Worker-ID: worker-a' \
  -o output.wav \
  -d '{
    "input": "Run only on worker-a",
    "voice": "speaker_a",
    "response_format": "wav"
  }'
```

指定 worker 后：

- worker 不存在时返回 `404`
- worker 未 ready 或 SGLang 不健康时返回 `429`
- worker 推理失败或断开时返回对应错误，不切换到其他 worker

### 使用 vLLM 兼容格式调用 IndexTTS-2.5

```bash
curl -X POST http://127.0.0.1:8080/v1/audio/speech \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -o output.wav \
  -d '{
    "model": "IndexTeam/IndexTTS-2.5",
    "input": "今天真是令人开心的一天！",
    "voice": "demo_voice",
    "speed": 1.5,
    "response_format": "wav",
    "extra_params": {
      "lang": "zh",
      "text_normalization": true,
      "emo_vector": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      "emo_alpha": 0.8
    }
  }'
```

支持直接使用 `ref_audio` 进行零样本克隆（无需预先保存音色）：

```bash
curl -X POST http://127.0.0.1:8080/v1/audio/speech \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -o output.wav \
  -d '{
    "model": "IndexTeam/IndexTTS-2.5",
    "input": "这是使用内联参考音频的零样本合成测试。",
    "ref_audio": "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEA...",
    "speed": 1.0,
    "extra_params": {
      "lang": "zhen",
      "text_normalization": true,
      "use_emo_text": true,
      "emo_text": "开心、兴奋而且充满活力",
      "emo_alpha": 0.8
    }
  }'
```

### 请求字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `input` | string | 是 | 要合成的文本，不能为空。支持 `[happy]你好[sad]世界[]恢复` 情绪标签 |
| `model` | string | 否 | 目标模型名称（如 `IndexTeam/IndexTTS-2.5`, `fishaudio/s2-pro`），未指定或不存在时自动使用默认模型 |
| `voice` | string | 否 | 已保存参考音色 ID |
| `voice_id` | string | 否 | `voice` 的兼容别名 |
| `ref_audio` | string | 否 | 零样本克隆音频（支持 data URL、base64、URL 或本地路径） |
| `references` | array | 否 | 兼容旧版内联参考音频列表 |
| `response_format` | string | 否 | 输出格式，例如 `wav`、`mp3`、`flac`，默认 `wav` |
| `stream` | boolean | 否 | 是否流式返回，默认 `false` |
| `speed` | number | 否 | 语速倍率（`0.5`–`2.0`），IndexTTS 默认原生映射为 `duration_factor = 1 / speed` 无损生成 |
| `duration_factor` | number | 否 | 模型内置时长系数（`0.5`–`2.0`，显式提供时与 `speed` 叠加） |
| `extra_params` | object | 否 | vLLM 标准 TTS 扩展参数字典（见下表） |

`extra_params` 扩展字段（也可在顶层扁平传递）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `lang` | string | 否 | 语种代码：`zh`, `en`, `ja`, `es`, `ar`，以及中英混合模式 `zhen`（不区分大小写），默认 `zh` |
| `text_normalization` | boolean | 否 | 是否启用文本正则化（数字/符号展开），默认 `true` |
| `emo_vector` | array | 否 | 8 维情感向量 `[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]` |
| `emo_alpha` / `emo_weight` | number | 否 | 情感控制强度系数，范围 `0.0`–`1.0`，默认 `0.8` |
| `emo_text` | string | 否 | 文本情感描述（如 `"开心、兴奋而且充满活力"`） |
| `use_emo_text` / `auto_emotion` | boolean | 否 | 是否根据文本自动推断情绪 |
| `emo_audio` | string | 否 | 独立情感参考音频（支持 data URL、base64、URL 或路径） |

#### 情感控制优先级（Precedence）
当同时提供多种情感控制来源时，生效优先级为：
`use_emo_text`（或文本情绪描述 / 标签） > `emo_vector` > `emo_audio` > 音色原本情感。

## `POST /v1/tts`

Fish Speech 兼容语音合成接口。内部会映射为 `/v1/audio/speech` 语义。

```bash
curl -X POST http://127.0.0.1:8080/v1/tts \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -o output.wav \
  -d '{
    "text": "[happy]Hello from fish tts endpoint",
    "model": "index-tts-2.5",
    "reference_id": "speaker_a",
    "format": "wav",
    "streaming": false,
    "speed": 1.25,
    "temperature": 0.8,
    "top_p": 0.8
  }'
```

字段映射：

| `/v1/tts` 字段 | `/v1/audio/speech` 语义 |
| --- | --- |
| `text` | `input` |
| `model` | `model` |
| `reference_id` | `voice` |
| `references` | `references` |
| `format` | `response_format` |
| `streaming` | `stream` |
| `prosody.speed` | Fish Audio 兼容语速倍率（`0.5`–`2.0`） |
| `speed` | 顶层语速倍率兼容写法，优先于 `prosody.speed` |
| `duration_factor` | `duration_factor` |
| `emo_audio` / `emo_audio_base64` | `emo_audio` |
| `emo_alpha` / `emo_weight` | `emo_alpha` |
| `emo_vector` | `emo_vector` |
| `use_emo_text` / `auto_emotion` | `use_emo_text` |
| `lang` | `lang` |
| 其他字段 | 原样透传给 worker |

`/v1/tts` 同样支持 `X-Fish-Worker-ID` 指定 worker。

## `POST /v1/references/add`

添加一个 manager 侧保存的参考音色。请求必须使用 `multipart/form-data`。

```bash
curl -X POST http://127.0.0.1:8080/v1/references/add \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -F 'id=speaker_a' \
  -F 'text=reference transcript' \
  -F 'audio=@reference.wav;type=audio/wav'
```

表单字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 参考音色 ID |
| `text` | string | 是 | 参考音频对应文本，不能为空 |
| `audio` | file | 是 | 参考音频文件 |

`id` 限制：

- 不能为空
- 长度不超过 255
- 只允许 ASCII 字母、数字、空格、`-`、`_`

成功响应：

```json
{
  "success": true,
  "message": "Reference voice 'speaker_a' added successfully",
  "reference_id": "speaker_a"
}
```

## `GET /v1/references/list`

列出所有已保存参考音色 ID。

```bash
curl http://127.0.0.1:8080/v1/references/list \
  -H 'Authorization: Bearer <OPENAI_API_KEY>'
```

成功响应：

```json
{
  "success": true,
  "reference_ids": ["speaker_a", "speaker_b"],
  "message": "Found 2 reference voices"
}
```

## `POST /v1/references/update`

重命名参考音色 ID。

```bash
curl -X POST http://127.0.0.1:8080/v1/references/update \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "old_reference_id": "speaker_a",
    "new_reference_id": "speaker_new"
  }'
```

成功响应：

```json
{
  "success": true,
  "message": "Reference voice renamed from 'speaker_a' to 'speaker_new' successfully",
  "old_reference_id": "speaker_a",
  "new_reference_id": "speaker_new"
}
```

## `DELETE|POST /v1/references/delete`

删除参考音色。

```bash
curl -X DELETE http://127.0.0.1:8080/v1/references/delete \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "reference_id": "speaker_new"
  }'
```

也兼容 `POST`：

```bash
curl -X POST http://127.0.0.1:8080/v1/references/delete \
  -H 'Authorization: Bearer <OPENAI_API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "reference_id": "speaker_new"
  }'
```

成功响应：

```json
{
  "success": true,
  "message": "Reference voice 'speaker_new' deleted successfully",
  "reference_id": "speaker_new"
}
```

## 调用顺序示例

1. 调用 `GET /health` 确认 manager 存活。
2. 调用 `GET /v1/workers` 确认至少有一个 `ready=true` 且 `sglang_healthy=true` 的 worker。
3. 调用 `POST /v1/references/add` 保存音色，或在合成请求中使用内联 `references`。
4. 调用 `POST /v1/audio/speech` 或 `POST /v1/tts` 获取音频。

## 内部接口

以下接口由 worker 使用，普通客户端不需要调用：

- `GET /internal/voices/:voice_id/audio`
- `GET /internal/workers/ws`

内部接口使用 `WORKER_TOKEN` 鉴权，不使用 `OPENAI_API_KEYS`。
