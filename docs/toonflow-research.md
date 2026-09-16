# Toonflow 画布节点调研（2026-09-16）

> 起因：我们的「资产表 / 资产设定图」节点与 Toonflow 的节点形态相似，想看看它（尤其是较新版本）是怎么处理画布节点的。
> 结论先说：**Toonflow 没有 v2/新版画布**，它是 1.x 持续迭代（本机装的是 v1.1.7，最新 Release 是 v1.1.8，2026-06-08）；节点设计在 `master` / `develop` 两个分支上完全一致（7 个节点组件文件的 SHA 逐一比对相同）。所以「新版效果更好」在节点设计层面并不成立——但那套节点做法确实有几处值得抄。

## 1. 版本与仓库（已核实）

| 项 | 值 |
| --- | --- |
| 主仓库 | `HBAI-Ltd/Toonflow-app`（Gitee 镜像同名），官网 `toonflow.net` |
| **画布前端源码仓库** | `HBAI-Ltd/Toonflow-web`（Vue 3 + Vue Flow） |
| 最新 Release | **v1.1.8**，2026-06-08（1.0.10 → 1.1.0 → … → 1.1.7(05-01) → 1.1.8(06-08)） |
| 本机安装版本 | **v1.1.7**（`ToonFlow.exe` 文件版本 1.1.7） |
| License 沿革 | v1.0.8 前后由 AGPL-3.0 改为 Apache-2.0 + 补充商业协议，声明不追溯 |

注意：网络上另有一个同名的「toonflow」卡通绘制/动画库，与本项目无关，别混。

## 2. 画布上有哪些节点（源码级核实）

画布在 `src/views/production/index.vue`，节点在 `src/views/production/node/`，**共 7 个组件，6 个启用，`poster` 被注释禁用**。

| 节点 | 端口 | 卡片上显示什么 | 可操作项 |
| --- | --- | --- | --- |
| `script` 剧本 | 2 个 source，无 target | 剧本正文 | 弹窗编辑 |
| `scriptPlan` 剧本规划 | target 左 / source 右 | Markdown 预览 | 点「编辑」→ **90vw 弹窗 + Markdown 编辑器（72vh，18 项工具栏）** |
| **`assets` 资产** | **只有 1 个 target（在顶部），没有 source** | **原始资产卡 → 箭头 → 衍生资产卡横排**；每卡 1:1 缩略图 + 名称 + 类型 tag（role/tool/scene/clip）+ 描述；状态（未生成/生成中/失败）画在缩略图区 | 点衍生卡开单图精修弹窗；衍生卡可删 |
| `storyboardTable` 分镜表 | target 左 / source 右 | Markdown 分镜表文本 | 同 `scriptPlan` 的大弹窗 |
| **`storyboard` 分镜图** | target 左 / source 右 | **分镜图网格**，每帧 200×gridScale；帧上有 `S01` 序号 tag、勾选框、hover 出编辑/删除、帧间 hover 出「＋」插入 | 网格缩放 0.1–3（localStorage 记住）、多选/全选/清空、批量删除、**网格预览（后端拼成一张大图）**、批量生成图片；单帧弹窗改 prompt |
| `workbench` 视频/工作台 | 只有 target 左 | 16:9 视频封面 + 播放按钮 | 点击整卡展开剪辑工作台 |
| `poster` 海报 | — | — | **UI 注册与连线都被注释，当前不可用** |

**拓扑是硬编码的，用户不能加节点**：

```
script ──(script-assets)──▶ assets              ← 资产挂在剧本下方
script ──(script-source)──▶ scriptPlan ──▶ storyboardTable ──▶ storyboard ──▶ workbench
```

关键：**「资产 → 分镜」在画布上没有连线**。资产节点是终端节点；分镜节点通过 `:assetsData="flowData.assets"` 这个 prop 拿到全部资产，再靠数据字段自己关联。

## 3. 角色/镜头是怎么关联的（这条最值得对比）

`src/views/production/utils/flowBuilder.ts` 里的数据结构：

```ts
AssetItem  { id, name, desc, prompt, src, state, type, flowId, derive: DeriveAsset[] }
Storyboard { id, duration, prompt, trackId, associateAssetsIds: number[], src, state, videoDesc, ... }
VideoList  { id, prompt, duration, storyboardId, trackId }
```

分镜记录里存 **`associateAssetsIds`（资产 id 数组）**；生成单帧时按这些 id 去顶层资产里找、找不到再去 `derive`（衍生图）里找，取到 `src` 后**自动塞进 `referanceImages`**。
→ 也就是说，Toonflow 的「参考图自动挂载」是**按主键查表**，不是按文件名或提示词里的名字。

| | Toonflow | 我们（B 期） |
| --- | --- | --- |
| 关联载体 | 分镜记录里的 `associateAssetsIds`（id 数组） | 提示词里的**资产中文名**（精确子串匹配） |
| 谁来建立 | 后端 Agent 写入（**具体怎么生成未核实**） | 运行时扫描该镜文本自动匹配 |
| 优点 | 明确、可编辑、可回溯 | 零维护：改提示词就改关联，不用维护 id |
| 缺点 | 用户看不到也改不了（藏在数据里），资产没归好就挂错 | 名字写错/同义说法就漏挂 |

另外两点：
- **单图精修是一张"子画布"**：`components/editImage/index.vue` 是全屏弹窗里内嵌的**第二张 Vue Flow 画布**，节点只有 `upload`（上传/参考图）和 `generated`（生成结果）两种，**连线即语义**——某 generated 节点的所有上游图片会被同步写入它的 `references`；这张子画布的图结构会持久化（`saveImageFlow` / `updateImageFlow` / `getImageFlow`），返回的 `flowId` 回写到资产/分镜记录里。
- 未核实项：后端如何产出 `associateAssetsIds`；`prompt` 里是否注入角色名做锚定；分镜→成片的拼接实现。

## 4. 技术栈与交互细节（已核实）

- **Vue Flow `@vue-flow/core` ^1.48** + `@vue-flow/background` / `controls` + `@dagrejs/dagre`；不是 React Flow，也不是自绘。UI 库是 TDesign Vue Next 1.18，另有 `md-editor-v3`、`monaco-editor`、`@webav/av-canvas`（剪辑）、`socket.io-client`（Agent 流式）。
- 画布参数：`min-zoom 0.1 / max-zoom 10`、**`only-render-visible-elements=false`（全渲染）**、`elevate-nodes-on-select`、禁用删除键与框选快捷键。
- **按住空格 + 左键可在节点上直接拖画布**；拖拽期间加 `is-interacting` 类降级渲染保帧，并显示 FPS 角标。
- 自动布局：LR 方向**手写排布**（主链等距 gap 80，`assets` 放 `script` 正下方并做重叠避让），TB 才用 dagre；布局前会轮询等所有节点 `dimensions` 测量完成且**连续两次快照一致**才动手，然后 `fitView`；拖拽后回写 `nodePositions` 防回跳。
- **属性面板不是常驻侧栏表单**，而是三件套：①节点内 hover 按钮；②右侧可折叠 Agent 聊天侧栏；③**弹窗**（长文本 90vw；单图精修全屏 + 内嵌画布）。

## 5. 值得抄的几处（按性价比）

1. **批量出图节点的多产物展示**
   - 资产节点：`原始卡 → 箭头 → 衍生卡横排`，天然表达「一个角色对多张设定图」。
   - 分镜节点：**固定网格 + 用户可调缩放系数（localStorage 持久化）**，让用户自己决定一屏看几张；再配「网格预览」——**后端把所选帧拼成一张大图**返回，避免前端同时渲染几十张缩略图。
   - 我们目前是浮框里一条横向缩略图带（44px），产物一多就挤成几行且看不出哪张是哪一镜。**建议：分镜图/资产图节点改成"网格 + 缩放 + 序号 tag"**。
2. **长文本不在节点里编辑，而是点开大弹窗**
   - 他们在节点上只渲染 Markdown 预览，编辑走 90vw 弹窗 + Markdown 编辑器（72vh）。
   - 我们目前只能在 150px 的预览里看，改内容要靠「补充要求」重跑（MEMORY 里记为遗留项）。**建议：文档节点加"点开大编辑器"**，把 `docText` 覆盖写回节点（`_node_inputs` 优先取它）。
3. **用一块区域同时表达"图 + 状态"**：缩略图区里生成中放 loading、失败放错误提示、未生成放空态，省掉一行状态。我们的做法是卡片角标 + 多缩略图，已经比较接近。
4. **嵌套子画布做单图精修**（连线即参考图），思路漂亮但工程量大，**不建议现在做**。
5. **性能**：`content-visibility: auto` 跳过长列表渲染、缩略图走后端 sharp 按 `?size=` 现场压缩并缓存；拖拽时降级渲染。我们的 `onlyRenderVisibleElements` 已经解决了大画布的主要问题。

## 6. 一个架构层面的差异（不必改，但要知道）

Toonflow 的画布是**固定 7 节点的"数据容器"**：id 固定、边硬编码、用户只能拖位置和改内容，拓扑由后端 Agent 驱动。
我们是**自由画布**：节点类型、连线、拓扑都由用户决定，外加「自动链」模板一键铺 L1/L2/L3。
两者取舍不同——他们的形态适合"一条流水线跑到底"的产品化体验，我们的形态适合"自己搭工作流"。**不建议为了像它而收窄成固定拓扑**；要抄的是节点卡片的呈现方式和长文本/多产物的交互。

## 7. 来源

- 官方仓库与 Release：`HBAI-Ltd/Toonflow-app`（GitHub / Gitee 镜像）、官网 `toonflow.net`
- 画布前端源码（本文档第 2/3/4 节的源码级结论均出自这里）：`HBAI-Ltd/Toonflow-web`
  - `src/views/production/index.vue`（主画布 / 自动布局 / 空格拖拽）
  - `src/views/production/utils/flowBuilder.ts`（节点、边、数据结构）
  - `src/views/production/node/*.vue`（7 个节点组件）
  - `src/views/production/components/editImage/index.vue`（单图精修子画布）
  - `package.json`（依赖与版本）
- 他人文章（可靠性中等，仅作旁证）：CSDN 上的快速上手教程、源码解析、产品拆解各一篇

> 调研方式说明：仓库为公开源码，本文档**只提炼交互与数据模型思路，不复制其代码**，实现仍按我们自己的 React Flow + 注册制契约来做。
