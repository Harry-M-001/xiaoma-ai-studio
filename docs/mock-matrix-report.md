# 本地模拟模型矩阵报告

- 日期：2026-11-22
- 模型：Seedream 5.5 / Seedream 5.0 Pro / Qwen-Image 2.1 × Seedance 2.0 / Seedance 2.5 / Wan 3.0
- 模拟服务：`backend/tools/mock_model_server.py`（8790）
- 矩阵脚本：`backend/tools/mock_matrix.py`（临时数据目录 + 8791 实例，不碰 8787）
- 全程只打本地模拟服务，图片与视频不产生真实模型费用；Agent 的规划使用 DeepSeek 文本模型。

## 一、画布链路（我直接驱动 API）

每格是一条「图片生成 → 视频生成（首尾帧，4 秒 / 16:9 / 720p）」画布，一次运行整图。

| # | 任务 | 图片模型 | 视频模型 | 调用 | 失败 | 产物 | 耗时 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 雨夜便利店 | Seedream 5.5 | Seedance 2.0 | 2 | 0 | 1 图 / 1 视频（4s） | 15.3s | 通过 |
| 2 | 清晨面馆 | Seedream 5.5 | Seedance 2.5 | 2 | 0 | 1 图 / 1 视频（4s） | 17.3s | 通过 |
| 3 | 沙漠公路 | Seedream 5.5 | Wan 3.0 | 2 | 0 | 1 图 / 1 视频（4s） | 16.5s | 通过 |
| 4 | 老式理发店 | Seedream 5.0 Pro | Seedance 2.0 | 2 | 0 | 1 图 / 1 视频（4s） | 16.8s | 通过 |
| 5 | 山间木屋 | Seedream 5.0 Pro | Seedance 2.5 | 2 | 0 | 1 图 / 1 视频（4s） | 17.1s | 通过 |
| 6 | 末班地铁 | Seedream 5.0 Pro | Wan 3.0 | 2 | 0 | 1 图 / 1 视频（4s） | 17.0s | 通过 |
| 7 | 夏夜天台 | Qwen-Image 2.1 | Seedance 2.0 | 2 | 0 | 1 图 / 1 视频（4s） | 17.3s | 通过 |
| 8 | 旧书摊相册 | Qwen-Image 2.1 | Seedance 2.5 | 2 | 0 | 1 图 / 1 视频（4s） | 17.7s | 通过 |
| 9 | 雪后寺庙 | Qwen-Image 2.1 | Wan 3.0 | 2 | 0 | 1 图 / 1 视频（4s） | 15.5s | 通过 |

模拟服务共记录 18 次请求（9 图 + 9 视频），鉴权头、`Content-Type`、`X-DashScope-Async` 全部合规，0 条异常。

## 二、Agent 对照（同样的 9 组模型与 9 句需求）

Agent 拿到的是同样一句话需求 + 指定的 `model_key`，由它自己读画布、查模型、生成草稿、落定、运行、等结果。

| # | 任务 | 图片模型 | 视频模型 | 步骤 | 工具调用 | 耗时 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 雨夜便利店 | Seedream 5.5 | Seedance 2.0 | 16 | 8 | 30.6s | 通过 |
| 2 | 清晨面馆 | Seedream 5.5 | Seedance 2.5 | 18 | 8 | 32.4s | 通过 |
| 3 | 沙漠公路 | Seedream 5.5 | Wan 3.0 | 18 | 8 | 33.4s | 通过 |
| 4 | 老式理发店 | Seedream 5.0 Pro | Seedance 2.0 | 18 | 8 | 31.1s | 通过 |
| 5 | 山间木屋 | Seedream 5.0 Pro | Seedance 2.5 | 18 | 8 | 35.2s | 通过 |
| 6 | 末班地铁 | Seedream 5.0 Pro | Wan 3.0 | 18 | 8 | 31.3s | 通过 |
| 7 | 夏夜天台 | Qwen-Image 2.1 | Seedance 2.0 | 18 | 8 | 32.1s | 通过 |
| 8 | 旧书摊相册 | Qwen-Image 2.1 | Seedance 2.5 | 18 | 8 | 33.5s | 通过 |
| 9 | 雪后寺庙 | Qwen-Image 2.1 | Wan 3.0 | 18 | 8 | 33.5s | 通过 |

9 次运行的流程完全一致：

```
get_canvas → list_models → draft_canvas → apply_canvas_draft
→ get_canvas → preview_canvas → run_canvas → wait_for_tasks
```

## 三、对比与结论

| 维度 | 我（直接调 API） | 工坊 Agent |
| --- | --- | --- |
| 规划 | 已知画布契约，直接写 2 节点 1 连线 | 要先读画布、查模型，再让 planner 生成草稿 |
| 调用次数 | 2 次（图 + 视频） | 2 次（图 + 视频）+ 8 次工具调用 |
| 单任务耗时 | 15–18s | 31–35s |
| 失败恢复 | 失败后由我读状态重试 | 有 `wait_for_tasks`、失败节点定位与重跑能力 |
| 可解释性 | 只有任务记录 | 每一步工具调用、参数、结果都落在时间线 |

结论：**执行链路的耗时几乎一样**（视频轮询 5 秒一次占大头），Agent 多出来的 15 秒左右是「读状态 + 规划 + 校验」的成本。这个成本是必要的——它换来了可续跑、可审计、失败可定位。真正该优化的不是模型调用，而是**别让 Agent 把轮数浪费在误判和重复查询上**。

## 四、这轮测试逼出来的真实问题（已修）

1. **`list_models` 不传能力只返回文本模型**：Agent 只查一次就得出「没有任何图片/视频模型」，9 个任务里有 5 个因此直接拒绝执行。改为不传 `modality` 返回全部。
2. **视频分辨率不在白名单**：草稿校验、`update_node` 都不认 `resolution`，Agent 反复补 720p / 1280x720，耗到轮数上限。三处一起放行，并在 `get_canvas` 里回显默认 720p。
3. **百炼视频永远「处理中」**：轮询读 `output.status`，文档字段是 `output.task_status`，任务完成也会一直等。两种写法都认。
4. **只说不做**：「我先确认 / 现在我用 / 先算成本 / 现在整图 / 现在落定」这类收尾没有触发续跑，任务停在半路。补齐识别词。
5. **同名模型跨服务串台**：`9:doubao-...` 被 planner 换成了 `1:doubao-...`，跑到另一家服务上。planner 提示词明确要求 `服务id:模型名` 原样照抄；矩阵脚本额外停用了真实图片/视频服务。
6. **等完还要再查三遍**：`wait_for_tasks` 已经带回最终状态，Agent 还在 `list_assets` / `get_asset` 逐个查产物。工具返回里直接写明「跑完就汇报，产物卡片在界面上」。
7. **轮数上限不够**：标准流程 8 轮正好卡死，调到 12。

修复前（第一轮）：9 个任务只有 2 个真正跑完；修复后：9/9 完成，18 次模拟调用，0 异常，且每次都是同一套 8 步流程。

## 五、复现

```bat
REM 只跑画布矩阵
backend\venv\Scripts\python.exe backend\tools\mock_matrix.py --mode canvas

REM 只跑 Agent 对照
backend\venv\Scripts\python.exe backend\tools\mock_matrix.py --mode agent
```

模拟服务单独启动（可选，脚本会自动起）：

```bat
backend\venv\Scripts\python.exe backend\tools\mock_model_server.py --port 8790
```

脚本会打印临时数据目录并保留 `app.log` / `mock_requests.jsonl`；加 `--keep` 可保留跑完后的数据库与产物。

> 提示：跑的时候别手动删临时目录（`xiaoma-mock-matrix-*`），数据库就在里面；删了会看到 `POST /api/projects -> 500`。
