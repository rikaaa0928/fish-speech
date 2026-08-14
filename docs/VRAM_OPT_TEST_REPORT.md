# fish-speech API Server 显存优化实测报告

- 日期：2026-08-13
- 分支/基线：`worker-api-server-backend`（HEAD `2bbb21b` + 未提交改动 9 个文件）
- 测试机：sc01（jupyter-llmtgh2w5rdw697q），RTX 3090 24 GB，256 核，Ubuntu 22.04
- 环境：`uv sync --extra cu128`（torch 2.8.0+cu128，Python 3.12.3），`PYTORCH_ALLOC_CONF=expandable_segments:True`
- 模型：`/root/rivermind-fs/models/s2-pro`（s2-pro，BF16 全量 ~9.1 GB + codec.pth）

---

## 1. 结论摘要

| 配置 | 稳态显存（nvidia-smi） | torch 峰值（peak_allocated） |
|---|---|---|
| 改动前（用户实测基线） | **19–20 GiB** | — |
| 改动后，cap=32768（仅 DAC 修复） | 15.90 GiB | 17.57 GiB |
| 改动后，cap=8192（全部修复） | **11.59 GiB** | **11.75 GiB** |

- **总节省 ≈ 7.4–8.4 GiB**（对比基线 19–20 GiB），与预期预算 ~7.36 GiB 一致。
- 其中 **LLAMA 上下文上限 8192 单独贡献 4.31 GiB**（同一份代码，仅 `--llama-max-seq-len` 不同，A/B 实测），**DAC 修复贡献约 3.0–3.5 GiB**。
- 启动全流程正常（LLAMA 加载 → KV cache → DAC → warmup → HTTP 服务），TTS 请求正常返回，请求后显存无增长。

---

## 2. 改动清单（本次实测覆盖）

| 文件 | 改动 |
|---|---|
| `fish_speech/models/dac/modded_dac.py` | ① 删除 32768×32768 `causal_mask` 预分配（~1 GiB/个），改为按实际位置动态生成；② RoPE `freqs_cis` 按 block_size 初始化 + `_ensure_freqs_cis_len` 按需扩展（Python int 入参，热路径无 GPU→CPU 同步） |
| `fish_speech/models/text2semantic/inference.py` | ③ `--llama-max-seq-len` 贯通到 `from_pretrained(max_length=...)`；④ 新增 `validate_llama_max_seq_len`（≥4096 且 8 的倍数）+ 拒绝超过 checkpoint 上限；⑤ 长度校验改为 `prompt+max_new_tokens>max_seq_len`；⑥ 线程初始化 try/except/finally，失败不再挂起；⑦ 启动日志输出 checkpoint_cap / 实际上限 / 预计 KV cache |
| `fish_speech/utils/schema.py` | `ServeTTSRequest.max_new_tokens` 加 `Field(ge=1)` |
| `tools/server/api_utils.py` | `--llama-max-seq-len` 参数 + parse 时校验 |
| `tools/server/model_manager.py` | 可复用 `log_gpu_memory(stage)` 四阶段打点 + 参数透传 |
| `fish-worker/*` | `API_SERVER_LLAMA_MAX_SEQ_LEN` 环境变量贯通，worker 部署默认 8192 |

---

## 3. 实测数据（修改后代码）

### 3.1 `--llama-max-seq-len 8192`（完整优化）

```
[gpu-mem] before model load       : allocated=0.00GiB reserved=0.00GiB peak=0.00/0.00GiB
LLAMA context: checkpoint_cap=32768 actual_max_seq_len=8192 estimated_kv_cache=1.12 GiB
[gpu-mem] after LLAMA load/cache  : allocated=9.75GiB reserved=9.75GiB peak=9.75/9.75GiB
[gpu-mem] after DAC load          : allocated=11.21GiB reserved=11.50GiB peak=11.49/11.50GiB
[gpu-mem] after warmup            : allocated=11.22GiB reserved=11.26GiB peak=11.72/11.75GiB
nvidia-smi (idle after startup)   : 11863 MiB
```

warmup：24 tokens / 3.35 s / 7.16 tok/s；生成期 torch 内部 `GPU Memory used = 12.63 GB`。

### 3.2 `--llama-max-seq-len 32768`（仅 DAC 修复，LLAMA 不缩）

```
LLAMA context: checkpoint_cap=32768 actual_max_seq_len=32768 estimated_kv_cache=4.50 GiB
[gpu-mem] after LLAMA load/cache  : allocated=14.06GiB reserved=14.08GiB peak=14.06/14.08GiB
[gpu-mem] after DAC load          : allocated=15.53GiB reserved=15.82GiB peak=15.81/15.82GiB
[gpu-mem] after warmup            : allocated=15.54GiB reserved=15.57GiB peak=17.54/17.57GiB
nvidia-smi (idle after startup)   : 16283 MiB
```

warmup 生成期 torch 内部 `GPU Memory used = 18.87 GB`。

---

## 4. A/B 分解

| 阶段 | 32768 | 8192 | Δ（LLAMA cap 贡献） |
|---|---|---|---|
| after LLAMA load/cache | 14.06 | 9.75 | **4.31 GiB** |
| after DAC load | 15.53 | 11.21 | 4.32 GiB |
| after warmup（allocated） | 15.54 | 11.22 | 4.32 GiB |
| after warmup（peak） | 17.57 | 11.75 | 5.82 GiB |

**4.31 GiB 差值的构成（估算）**：
- 慢模型 KV cache：4.50 → 1.12 GiB，约 3.38 GiB
- 快模型 KV cache 的序列长度固定为 `num_codebooks`，不随 LLAMA 上下文上限变化，A/B 中可视为不变
- LLAMA `causal_mask`（`torch.tril(ones(max_seq_len,max_seq_len,bool))`）：1.0 → 0.06 GiB，约 0.94 GiB
- 合计 ≈ 4.31 GiB，与实测差值一致

**DAC 修复贡献**：基线 19–20 GiB → 32768 配置 15.90 GiB（nvidia-smi），约 **3.0–3.5 GiB**（删除 ~3 GiB causal_mask + RoPE 缩小）。该配置仍含优化后代码，故 32768 不能完全等于改动前基线，此处为下限估计。

**总账**：基线 19–20 → 8192 配置 11.59 GiB，**节省 ~7.4–8.4 GiB**。

---

## 5. 功能回归验证

| 项目 | 结果 |
|---|---|
| `/v1/health` | 200 `{"status":"ok"}` |
| `/v1/tts`（中文长句） | 200，344,108 字节 WAV（RIFF/PCM/16bit/mono/44.1kHz），3.90 s 音频，10.3 s 墙钟，85 tokens @ 8.60 tok/s |
| 请求后显存 | 11863 → 11863 MiB（无增长，KV cache 预分配复用，符合预期） |
| 生成格式/时长 | 波形格式正确，时长与 token 数线性匹配（200 tokens→3.9 s）；本轮未做主观音质评价或波形差分 |

---

## 6. 错误路径验证

| 场景 | 结果 |
|---|---|
| `--llama-max-seq-len 65536`（超 checkpoint 上限） | **快速失败不挂起**（秒退）：`ValueError: llama_max_seq_len=65536 exceeds the checkpoint max_seq_len (32768); refusing to expand the context.` —— 验证了 cap 校验 + 线程初始化异常重抛修复 |
| `max_new_tokens=40000`（prompt+gen > 上限） | 清晰报错：`Requested sequence exceeds the LLAMA context limit: prompt=21 tokens + generation=40000 tokens = 40021, but max_seq_len=32768. Maximum acceptable generation length here: 32747 tokens.` |
| 非法 cap（<4096 / 非 8 倍数） | `validate_llama_max_seq_len` 在 parse 阶段即拒绝（代码路径，未单独跑） |

> 2026-08-14 最终回归已将明确的上下文长度超限改为 HTTP 422；其他内部推理异常仍返回 500。

---

## 7. 遗留事项与注意事项

1. **codec BF16 已做成可选项**：默认仍为 FP32；仅显式设置 `API_SERVER_DECODER_DTYPE=bfloat16` 时启用。最终回归见第 9 节。
2. **DAC encoder 常驻**：按实际模型参数统计，单独删除 encoder 仅省 293 MiB（FP32）/147 MiB（BF16）。完整 decode-only 还可删除 quantizer 的 downsample + pre_module，合计约省 789 MiB（FP32）/395 MiB（BF16），但会禁用参考音频克隆和 `/v1/vqgan/encode`，本轮不实施。
3. **仍未回归项**：8192 边界附近的成功请求、`/v1/references/*`、`--half`、多 worker。长参考音频、`--compile`、VQGAN encode/decode 已在最终回归覆盖。
4. **`.git` 未随代码 scp**：远程 `/root/fish-speech` 是纯源码树，若运行 `autodl_start_worker.sh` 的 git pull 会自动跳过（无 .git），不会覆盖本地改动。
5. **端口**：测试用 8000/8001；worker 默认 8000（`start.sh`）。
6. **部署注意**：worker 部署默认 `API_SERVER_LLAMA_MAX_SEQ_LEN=8192`；要保留 checkpoint 原始 32768 需显式设空 `API_SERVER_LLAMA_MAX_SEQ_LEN=`。

---

## 8. 复现命令

```bash
# 环境（已在 /root/fish-speech 就绪）
cd /root/fish-speech
export PYTHONPATH=/root/fish-speech PYTORCH_ALLOC_CONF=expandable_segments:True
V=./fish-worker/.venv/bin/python

# 完整优化（8192）
$V -m tools.api_server \
  --llama-checkpoint-path /root/rivermind-fs/models/s2-pro \
  --llama-max-seq-len 8192 \
  --decoder-checkpoint-path /root/rivermind-fs/models/s2-pro/codec.pth \
  --decoder-config-name modded_dac_vq \
  --device cuda --listen 0.0.0.0:8000

# A/B 对照（32768，仅 DAC 修复）
# 同上但 --llama-max-seq-len 32768 --listen 0.0.0.0:8001

# 请求
curl -X POST http://127.0.0.1:8000/v1/tts -H "Content-Type: application/json" \
  -d '{"text":"测试语音","references":[],"max_new_tokens":200,"format":"wav"}' -o out.wav
```

---

## 9. 2026-08-14 最终综合回归

- 测试源码：`worker-api-server-backend`，最终 HEAD `fd494e9`
- 测试目录：远程独立源码树 `/root/fish-speech-final-test-20260814`，未覆盖正式目录
- GPU：RTX 3090 24 GB
- LLAMA：`max_seq_len=8192`，FP32/BF16 codec 两轮均覆盖 `--compile`
- 参考音频：83.52 秒、44.1 kHz、单声道 PCM

### 9.1 显存与长参考克隆

| codec dtype | DAC 加载后 allocated | warmup 后 allocated/reserved | nvidia-smi | 长参考克隆 |
|---|---:|---:|---:|---|
| FP32（默认） | 11.21 GiB | 11.22 / 11.25 GiB | 11,860 MiB（TTS 后） | HTTP 200，942,124 字节 WAV，9.57 s 墙钟，FFmpeg 完整解码 |
| BF16（可选） | 10.48 GiB | 10.50 / 10.59 GiB | 11,534 MiB（TTS/API 后） | HTTP 200，1,007,660 字节 WAV，8.49 s 墙钟，FFmpeg 完整解码 |

BF16 在加载和 warmup 阶段的 allocated 比 FP32 低约 0.73 GiB。表中的 nvidia-smi 数值采自不同 API 序列之后，受 CUDA allocator reserve 影响，不作为严格 A/B 差值。两种精度使用相同参考、文本、seed 和采样参数，但生成 token 序列不同，因此只可认为本轮主观听感无明显退化，不是数学意义的无损或波形等价。

### 9.2 API 与错误路径

| 项目 | FP32 | BF16 |
|---|---|---|
| `/v1/health` | 200 | 200 |
| 长参考 `/v1/tts` | 200，WAV 可完整解码 | 200，WAV 可完整解码 |
| 流式 `/v1/tts` | 200，`RIFF` header，FFmpeg 可解码 | 200，`RIFF` header，FFmpeg 可解码 |
| 上下文超限 | HTTP 422，包含 prompt/gen/limit 明细 | HTTP 422 |
| `/v1/vqgan/encode`（3 秒参考） | 200，tokens `[10, 65]` | 200，tokens `[10, 65]` |
| `/v1/vqgan/decode` | 200，266,240 字节 PCM | 200，266,240 字节 PCM |

最终测试额外发现并修复：流式 WAV header 被异步层过滤、DAC VQ decode 调用了不兼容签名、BF16 tensor 不能直接转换为 NumPy。修复后两种 codec dtype 的完整矩阵均通过，测试服务已停止，GPU 已释放。

---

## 10. 生成长度与截断语义验证

测试配置为 FP32 codec、`llama_max_seq_len=8192`，使用同一参考音频、文本模板和 seed。普通中文文本不会由 API server 自动拆分，因此这里验证的是单次生成的完整性。

| `max_new_tokens` | 输入长度 | HTTP | 生成 tokens | 音频时长 | 结果 |
|---:|---:|---:|---:|---:|---|
| 1024 | 149 字 | 200 | 898 | 41.66 s | 正常 EOS，完整 |
| 1024 | 170 字 | 206 | 1024 | 47.55 s | 触顶，保留已生成 WAV |
| 2048 | 304 字 | 200 | 1783 | 82.76 s | 正常 EOS，完整 |

结论：`8192 + 1024` 的实际瓶颈是生成 token 上限，完整中文长度仍约 150 字量级，并没有相对旧配置获得有意义的提升。保持 LLAMA cap 为 8192、把 `max_new_tokens` 提高到 2048 后，同一模板可完整处理约 300 字，处理长度接近翻倍；本轮 2048 请求的生成期累计峰值约 18.36 GB，完成后 nvidia-smi 回落到约 11.6 GiB。

截断现在采用以下非流式响应语义：

- 正常 EOS：HTTP 200，`X-TTS-Finish-Reason: stop`
- 达到生成上限：HTTP 206，`X-TTS-Finish-Reason: length`
- 两者均返回可播放音频，并带 `X-TTS-Generated-Tokens`、`X-TTS-Max-New-Tokens`、`X-TTS-Input-Characters`

流式响应在生成完成前已经发出 HTTP 状态，无法在末尾改为 206，因此本轮只对非流式请求提供 206 语义。

### 10.1 上下文与生成上限扩展矩阵

进一步使用同一段 83.52 秒参考音频测试 `max_seq_len=4096/8192/16384`。该参考音频本身编码为 1799 个语义 token，因此较小上下文会明显挤占输出空间。

| `max_seq_len` | `max_new_tokens` | 输入 | 结果 | 生成/音频 | 生成期峰值或失败点 |
|---:|---:|---:|---|---|---|
| 4096 | 1800 | 300 字 | HTTP 200，完整 | 1781 tokens / 82.66 s | 17.22 GB；warmup allocated 10.61 GiB |
| 4096 | 3072 | 450 字 | HTTP 422 | prompt 2349 + generation 3072 > 4096 | 生成前拒绝 |
| 8192 | 3072 | 450 字 | HTTP 200，完整 | 2468 tokens / 114.57 s | 17.83 GB，36.30 tok/s |
| 8192 | 4096 | 600 字 | HTTP 200，完整 | 3338 tokens / 154.97 s | 23.88 GB，36.39 tok/s |
| 8192 | 5120 | 750 字 | HTTP 500，OOM | 语义阶段已生成 4048 tokens | DAC 解码需再分配约 1.48 GiB，仅余约 1.17 GiB |
| 16384 | 3072 | 450 字 | HTTP 200，完整 | 2664 tokens / 123.67 s | 19.24 GB，31.18 tok/s |
| 16384 | 4096 | 600 字 | HTTP 200，完整 | 3234 tokens / 150.14 s | 24.33 GB，31.10 tok/s |
| 16384 | 5120 | 750 字 | HTTP 500，OOM | 语义阶段已生成 4089 tokens | DAC 解码需再分配约 1.50 GiB，仅余约 1.31 GiB |

结论：在 24 GB RTX 3090、FP32 codec、83.52 秒参考音频条件下，`8192 + 4096` 是本轮测得的实用上限，可完整处理约 600 字。`4096` 仅少约 0.6 GiB 启动驻留，却因长参考占用 prompt 而把可处理长度压回约 300 字；`16384` 比 8192 多约 1.3 GiB 驻留、速度下降约 14%，没有提高已验证的完整文本长度。750 字档的瓶颈已经变为一次性 DAC 解码，而不是 LLAMA context；继续增大到 32768 只会增加常驻显存，因此未重复执行必然更不利的 5120 压力档。
