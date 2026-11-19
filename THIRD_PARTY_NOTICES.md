# 第三方组件声明（THIRD PARTY NOTICES）

小马AI工坊（XiaoMa AI Studio）本体遵循仓库根目录的 `LICENSE`（PolyForm Noncommercial 1.0.0）。
本文件列出**可选组件**用到的第三方软件；分发本项目（含便携包）时请随附本文件。

---

## DeepSeek Harness（dsh）

- 包名：`@deepseek-ai/dsh`
- 版本：`0.2.2-rc.1`（由「本机引擎 → DeepSeek Harness」固定版本安装）
- 用途：可选的高级模式 sidecar；不安装、不启动时本项目不依赖它
- 许可：MIT License
- 版权：Copyright (c) 2026 DeepSeek
- 来源：<https://github.com/deepseek-ai/deepseek-harness>

本项目不隶属于 DeepSeek，也不暗示其官方背书；产品名不使用其商标。
dsh 由用户在自己机器上按需下载，本项目不经手其计费。

```text
MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

dsh 自身还依赖若干 npm 包，它们各自携带许可文件，安装后位于
`<数据目录>/harness/dsh/node_modules/` 下。

## Node.js 运行时

- 版本：`24.x` LTS（优先使用系统安装的 Node；没有时由可选组件下载官方 zip，并用官方 `SHASUMS256.txt` 校验 sha256）
- 许可：MIT 及随附的第三方许可
- 来源：<https://nodejs.org/>
- 说明：Node 的完整许可与第三方声明随官方发行包一起下载，位于解压后的
  `<数据目录>/harness/node/LICENSE` 与同目录的许可文件中。
