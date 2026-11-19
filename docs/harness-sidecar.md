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
- 分发时附：dsh 的 MIT LICENSE、[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)、我们的集成说明。
  界面不额外做来源标注；声明随分发包与仓库一起提供。
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
| P0 ✅ | 复现试点：HTTP MCP + 只读工具 | 已在仓库实现 `/api/mcp`，四个只读工具；实测 initialize / tools/list / list_projects / preview 全部可用 |
| P1 ✅ | 阻塞式确认门 | 已实现：run 调用挂起 → 全局确认卡 → 拒绝/超时都不执行；实测拒绝返回 `ok:false`、重复确认 409 |
| P2 ✅ | 可选组件打包 | 已实现「本机引擎 → DeepSeek Harness」卡片：一键安装（Node + dsh）、启动/停止、打开带 token 的页面；未安装/未启动时原 Agent 不受影响 |
| P3 ✅ | 文件与 Shell | 已实现工作区选择与校验：dsh 以工作区为 cwd、强制 `workspace-write` + `ask`，越界写入由沙箱拒绝；不能在界面里逐条预审命令（执行时由 dsh 的审批浮层逐条询问） |

## 十、未决问题

1. 「高级模式」入口放哪：Agent 页右上角，还是独立页？
2. dsh 的 Web UI 是嵌入 iframe 还是新窗口打开（涉及端口与静态资源）。
3. 是否允许 dsh 直接改画布 JSON 文件（不推荐；仍走 MCP）。
4. Windows 便携包里 Node 运行时的体积与下载源。
5. 与「原子节点 + 组合原语」的先后：**组合原语已完成（图片组），dsh 已接上 P0/P1**。

## 十一、P0/P1 落地记录（2026-11-19）

**MCP 服务端**（`backend/app/routers/mcp.py`）

- `POST /api/mcp`：手写的最小 MCP JSON-RPC（initialize / ping / tools/list / tools/call / notifications），
  返回 `application/json`；无状态，不依赖 SSE。
- 工具面：`xms_list_projects` / `xms_get_canvas` / `xms_summarize_canvas` / `xms_preview_canvas`
  （只读）与 `xms_run_canvas`（阻塞式确认）。读工具直接复用 `agent_tools` 的实现，不抄第二份。

**确认门**（`backend/app/services/mcp_gate.py`）

- `xms_run_canvas` 先算 preview，再把调用落成 `agent_steps` 里一条 `pending`（挂在
  「dsh 高级模式」会话下，同一张画布复用同一条会话），然后**挂起等待**。
- 界面接口：`GET /api/mcp/confirmations`（轮询）、`POST /api/mcp/confirmations/{step_id}`
  （`{approve: bool}`）。**审批接口不在 MCP 工具清单里**，模型拿不到。
- 拒绝 / 超时（120 秒）/ 进程退出都按「没执行」返回；重复确认返回 409，
  不存在二次执行的可能。
- 批准后按「确认时看到的调用数」放行预算闸（`ack_calls`），再执行 `run_canvas`。

**界面**（`frontend/src/components/McpGate.tsx`）

- 挂在应用外壳上全局轮询，任何页面都能弹出确认卡；关掉卡片不会执行，
  调用继续挂到超时。卡片上显示调用次数、会重做的节点、被阻塞的节点与剩余秒数。

**验证**

- `backend/tests/test_mcp.py`：协议形状、只读调用、批准才执行、拒绝/超时不执行、
  会话复用、重复确认 409、前端挂载与轮询。
- 本机实测：`tools/list` 五个工具；对 18 号画布发起 run → 确认卡出现（calls 0 / blocked 1）
  → 拒绝 → MCP 返回 `{"ok": false, "error": "用户拒绝了这次执行，没有派发任何任务。"}`。

**当前边界**

- 接口层**强制只接受本机连接**（非 `127.0.0.1` / `::1` 一律 403）；P1 还没有 token，
  即使把 `HOST` 改成 `0.0.0.0`，MCP 与确认接口也不会跟着暴露。
- `run_node` 仍未开放（按设计等确认门跑顺再放）。
- 文件 / Shell / 子 Agent 的沙箱与逐条审批属于 P3，还没接。

## 十二、P2 落地记录（2026-11-19）

**组件生命周期**（`backend/app/services/harness.py` + `backend/app/routers/harness.py`）

- 数据目录下 `harness/`：`node/`（必要时下载的便携 Node）、`dsh/`（固定 `0.2.2-rc.1`）、
  `home/`（`DSH_HOME`）、`dsh.log`、`dsh.pid`。主包不内置任何一项。
- 安装是一个后台任务（`POST /api/harness/install`）：优先用 ≥24.9 的系统 Node；
  没有就下载 Node 官方 zip，并用同目录 `SHASUMS256.txt` 校验 sha256；随后
  `npm install --prefix` 固定版本 dsh。状态通过 `GET /api/harness/status` 轮询。
- 启动（`POST /api/harness/start`）：先确保 `profiles/web` 已初始化，再把
  `- insert:` 形式的 MCP 补丁写进 profile 的 `cordis.patch.yml`；`DEEPSEEK_API_KEY`
  只进子进程环境（从「模型服务」里的 DeepSeek 官方服务解密取得）。默认端口 **3081**
  （避开本机可能已经在跑的 3080 实例）。
- dsh 启动后会把带 token 的地址打印到日志；状态接口把**带 token 的完整 URL** 返回给
  「打开」，无 token 的根路径会 401。停止只杀自己写过 pid 的进程；端口上是别处起的
  dsh 时只给「打开」，不给「停止」。

**界面**

- 「本机引擎」页新增 DeepSeek Harness 卡片：Node / dsh / Key 三个状态、安装进度、
  启动/停止/打开、最近日志。文案写明「不装、不启动时，原 Agent 完全不受影响」。

**验证（本机）**

- 安装：系统 Node 24.18.1 + npm，约 2.5 分钟装好 `@deepseek-ai/dsh@0.2.2-rc.1`。
- 启动：3081 就绪，`owned=true`；日志给出 `?token=…` 地址；停止后 `running=false`。
- 挂载：dsh 启动时确实向 `/api/mcp` 发了 initialize / tools/list（后端日志可见），
  说明 MCP 补丁生效。

## 十三、P3 落地记录（2026-11-19）

**工作区就是写入边界**

- 启动前必须先选工作区（`POST /api/harness/workspace`，保存在 `harness/settings.json`）。
  校验规则：绝对路径、真实存在的目录；拒绝相对路径、不存在的路径、磁盘根、仓库根及其
  上级、数据目录及其上级。
- 启动时把这个目录作为 dsh 进程的 `cwd`：dsh 的 `sandbox-policy` 默认
  `workspaceRoot = process.cwd()`，所以「会话不可变 cwd」天然就是工作区；越界写入由
  `dsh-fs-sandbox` / `dsh-tool-pwsh` 在调用层拒绝。
- **不继承用户的试验配置**：启动时强制 `DSH_PERMISSION_MODE=workspace-write`，
  即使环境里留过 `danger-full-access` 也不会被悄悄带上；审批策略随之为 `ask`
  （dsh 的默认组合：非 full-access 时逐条询问）。
- `DSH_HOME` 与工作区分开：会话日志、profile、插件仍放在 `harness/home`，不会被
  工作区的清理动作误删。

**界面**

- Harness 卡片新增工作区输入与保存按钮；未就绪时「启动」按钮置灰并给出原因；
  权限口径直接写在状态里（「只写工作区 · Shell 命令逐条确认」）。

**验证（本机）**

- 拒绝：仓库根、`backend/data`、`D:\\` 均返回 400，错误文案分别指出原因。
- 通过：`D:\\ai-canvas-github\\dsh-workspace-test` 保存成功，状态 `ready=true`；
  启动 3081 成功，停止正常。
- 单元测试覆盖工作区校验、保存/读回、状态字段与前端入口。

**仍未做**

- 界面里**不逐条预审命令**：命令执行的审批在 dsh 自己的浮层里完成（`ask`），我们
  只负责把权限口径钉死在安全的一档。
- 目录选择器是 P3.1 已补的一层只读浏览（`GET /api/harness/dirs`，只列子目录），
  不能在这里预览或编辑文件。


