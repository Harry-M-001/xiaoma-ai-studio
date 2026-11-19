# DeepSeek Harness（dsh）sidecar 集成设计

> 结论先行：**不替换我们的 Agent 主循环，把 dsh 作为「高级模式」sidecar**。
> 画布、组合原语、预算与确认门留在我们的后端；文件、Shell、代码搜索、子 Agent 交给 dsh。
> 本文基于 2026-11-19 的本机试点（dsh `0.2.2-rc.1`）写成，试点脚本不在仓库内。

---

## 一、为什么是 sidecar，而不是替换

| 维度 | 换成 dsh 主循环 | sidecar（本文方案） |
|---|---|---|
| 画布工具 | 24 个工具要全部重写成 dsh 插件 | 包一层 HTTP MCP，零重写 |
| 确认门 | 要在 dsh 的权限模型里重建「跑整图几次调用」 | 工具调用阻塞等人点，语义不变 |
| 会话/步骤 | 现有 `agent_sessions/agent_steps` 要迁移 | 保持不动，按需引用 dsh 轨迹 |
| 打包 | 必然引入 Node 运行时 | 可选下载，主包不变重 |
| 风险 | preview 阶段的破坏性变更会直击主流程 | 坏了就关掉，原 Agent 仍在 |

一句话：**dsh 补的是「环境动手能力」，我们守的是「画布与花钱的边界」。**

## 二、试点结论（事实）

1. dsh `0.2.2-rc.1`，Node 24 下可跑；`dsh headless` 能完成单任务并输出结果。
2. MCP 接入可用。工具名为 `mcp__xms__<tool>`；**配置必须写在 `- insert:` 下**，写成顶层同 id 覆盖会静默不生效。
3. 实测：用库里 DeepSeek Key，dsh 成功调用 `xms_list_projects`（16 张画布）与 `xms_preview_canvas`（14 号画布 2 次调用 / 2 个重做 / 0 阻塞），数字与后端逐字一致。
4. **沙箱边界是硬约束**：MCP 子进程直接读 SQLite 被拒（WAL 需要写权限）。首次 MCP 未挂载时，dsh 自行把 `xiaoma.db` 与 `secret.key` 复制进工作区绕过——能力很强，但这是必须堵住的口子。
5. **模型兼容要逐个验**：KIE 的 OpenAI 兼容端点流式返回 `Stream ended without finish_reason`；DeepSeek 官方端点正常。
6. 许可：MIT，可商用；分发需保留 LICENSE 与 `THIRD_PARTY_NOTICES.md`，不授予商标权。developer preview 阶段必须锁版本。

## 三、目标架构

```
┌──────────────────────────── 我们的 FastAPI（唯一事实源） ────────────────────────────┐
│ 画布 / 节点 / 组合原语   预算闸   确认门（人点）   会话与步骤   资产库                │
│            ▲                     ▲                                                 │
│            │ HTTP MCP（127.0.0.1，沙箱外）                                            │
└────────────┼─────────────────────┼─────────────────────────────────────────────────┘
             │                     │ 阻塞式确认（人点后返回）
      ┌──────┴─────────────────────┴──────┐
      │ dsh sidecar（Node，锁版本，可选）  │
      │ 文件 · Shell · 搜索 · 子 Agent · 轨迹 │
      └───────────────────────────────────┘
```

- **传输选 HTTP（streamable-http），不用 stdio**：stdio 子进程会被 dsh 的沙箱限制，且每次会话起一个 Python 进程；HTTP 常驻在我们的服务里，天然在沙箱外，也便于做鉴权与审计。
- dsh 不直接碰 `backend/data`；它只通过 MCP 工具访问画布。

## 四、工具面设计

### 4.1 暴露给 dsh 的工具（第一批）

| 工具 | 权限 | 说明 |
|---|---|---|
| `xms_list_projects` | 只读 | 列画布 |
| `xms_get_canvas` | 只读 | 读节点/连线/状态 |
| `xms_summarize_canvas` | 只读 | 摘要，省 token |
| `xms_preview_canvas` | 只读 | 代价预估 |
| `xms_run_canvas` | **阻塞式确认** | 调用后挂起，等界面点确认；拒绝/超时返回 `ok:false` |

**不暴露**：
- 任何「签发执行令牌」的工具（否则模型可自批）
- 任意 SQL / 任意文件读写
- `run_node` 暂不开放，等确认门跑顺再放

### 4.2 确认门：阻塞式，而不是令牌

```
dsh → xms_run_canvas(project_id)
        ↓ 后端创建一条 pending 确认（复用 agent_steps 的 pending 语义）
        ↓ 前端轮询到确认卡：几次调用 / 几个重做 / 几个阻塞
用户点「确认」→ 工具返回执行结果；点「取消」→ 返回 {ok:false, error:"用户拒绝"}
        ↓ 超过 toolCallTimeoutMs（建议 120s）→ 返回超时，不执行
```

要点：
- **令牌永远不经过模型**；模型只能「发起」，不能「批准」。
- 一个工具调用最多挂 120s；dsh 的 `toolCallTimeoutMs` 可配。
- 同一条回复里排在后面的调用照旧记 `skipped`，与现有实现一致。

## 五、权限与沙箱

1. **工作区隔离**：dsh 的 workspace 只允许是用户显式选的「项目文件夹」，**禁止**指向仓库根或 `backend/data`。
2. **禁写数据目录**：不把 `backend/data` 加入 `--add-dir`；MCP 在服务端进程里读库，不需要 dsh 有文件权限。
3. **Shell 审批**：Web 模式用 `ask`，不默认 `never`；高风险命令（删除、网络下载、安装）逐条确认。
4. **凭据**：`DEEPSEEK_API_KEY` 只进进程环境；dsh 的 stdio 桥会剥离名字像凭据的环境变量，别指望透传。
5. **审计**：MCP 侧记录每次调用的 `session_id / 工具 / 参数摘要 / 结果状态 / 耗时`；dsh 侧保留 append-only 轨迹，两边用任务 id 关联。

## 六、配置与版本

```yaml
# $DSH_HOME/profiles/<profile>/cordis.patch.yml
- insert:
    - id: xms
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: xms
        transport: streamable-http
        url: http://127.0.0.1:8787/api/mcp
        toolCallTimeoutMs: 120000
```

- **锁版本**：固定 `@deepseek-ai/dsh@<精确版本>`，升级走显式操作；每次升级跑一遍工具清单与确认门回归。
- **模型**：首选 DeepSeek 官方端点；其它 OpenAI 兼容端点需过「流式 + tool_calls」兼容测试后再列进可选清单。
- **离线**：dsh 与 Node 运行时均按需下载，装到数据目录（与「本机引擎」同一套下载/校验/删除机制），主便携包不内置。

## 七、打包与许可

- 可选组件包内容：Node 运行时（LTS）+ 固定版本 dsh + 启动脚本；下载后 sha256 校验。
- 分发时附：dsh 的 MIT LICENSE、`THIRD_PARTY_NOTICES.md`、我们的集成说明；界面标注「由 DeepSeek Harness 提供」。
- 不暗示 DeepSeek 官方背书；不使用其商标做产品名。
- 用户 Key 仍由用户自备（DeepSeek 或其它），我们不经手计费。

## 八、降级与失败

| 情况 | 行为 |
|---|---|
| dsh 未安装 / 启动失败 | 「高级模式」置灰并指路；原 Agent 不受影响 |
| MCP 连不上 | dsh 会话内提示；不自动重试超过 2 次 |
| 工具超时 | 返回超时，未执行；前端标记「未确认」 |
| 版本不兼容 | 拒绝启动并给出「回退到 X 版本」的指引 |
| 模型不支持工具调用 | 会话开始前拦截，提示换模型 |

## 九、分期与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 复现试点：HTTP MCP + 只读工具 | dsh 能列画布、读画布、报代价，数字与后端一致 |
| P1 | 阻塞式确认门 | 模型发起 run → 界面弹卡 → 取消不执行、确认才执行；模型无法自批 |
| P2 | 可选组件打包 | 一键下载/校验/启动/停止；关掉后原 Agent 正常 |
| P3 | 文件与 Shell | 限定工作区；危险命令逐条确认；越界写入被拒 |

## 十、未决问题

1. 「高级模式」入口放哪：Agent 页右上角，还是独立页？
2. dsh 的 Web UI 是嵌入 iframe 还是新窗口打开（涉及端口与静态资源）。
3. 是否允许 dsh 直接改画布 JSON 文件（不推荐；仍走 MCP）。
4. Windows 便携包里 Node 运行时的体积与下载源。
5. 与「原子节点 + 组合原语」的先后：建议组合原语先做，dsh 后接——否则 dsh 只能操作旧节点。
