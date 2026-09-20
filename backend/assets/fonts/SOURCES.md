# 随包字体与可按需下载的字体

这些字体用于把字幕烧进视频。**全部是 SIL OFL 1.1**，允许再分发（分发时需附授权文本，见 `licenses/`）。

## 内置（随仓库与便携包分发，约 33 MB）

| ASS 里写的族名（`Fontname`） | 文件 | 体积 | sha256 |
|---|---|---|---|
| `Noto Sans CJK SC` | `NotoSansCJKsc-Regular.otf` | 16,437,364 B（15.68 MiB） | `2c76254f6fc379fd…` |
| `Noto Sans CJK SC` | `NotoSansCJKsc-Bold.otf` | 17,002,248 B（16.21 MiB） | `b5f0d1a190a7f9b4…` |

覆盖范围：GB18030 全部汉字 + 通用规范汉字表 8105 字 + 繁体大五码。
两个字重**同族名**（靠字重区分），所以两份都要在，`Bold=1` 时才会用到粗的那份。

内置一款是为了让「打开就能烧中文字幕」这条成立——这是唯一随包分发的中文字体。

## 按需下载（**不随包分发**，在应用里点一下下到数据目录）

| 字体 | 文件 | 体积 | sha256 | 用途 | 覆盖范围 |
|---|---|---|---|---|---|
| 思源宋体 | `NotoSerifCJKsc-Regular.otf` | 24,543,080 B（23.41 MiB） | `2a2eae2628df8355…` | 宋体 · 叙事 | 65 535 字形 / 总字数 458 745，含全部大五码 |
| 霞鹜文楷 | `LXGWWenKai-Regular.ttf` | 25,575,676 B（24.39 MiB） | `39ad71264b588165…` | 楷体 · 古风 | GB 2312 全部 6763 字 + 通用规范汉字表，另补扩展 A 区与部分扩展 B 区 |
| 得意黑 | `SmileySans-Oblique.ttf` | 2,629,764 B（2.51 MiB） | `b447d7e781f08bc9…` | 标题 · 美术字 | **只覆盖通用规范汉字表 8105 字**（简体），繁体与生僻字会缺 |

托管在 `fonts-v1` 这个 Release 的附件上（**单独一个 Release，地址长期不变**——挂在版本
Release 下的话每次发版地址都变，而地址是写在代码里的）：

- `https://gitee.com/haoruiM/xiaoma-ai-studio/releases/download/fonts-v1/{文件名}`（优先，国内可达）
- `https://github.com/Harry-M-001/xiaoma-ai-studio/releases/download/fonts-v1/{文件名}`

**为什么不用上游直链**：`raw.githubusercontent.com` 国内基本不通，而用户在国内。

下载后落在**数据目录**的 `fonts/` 下（不是仓库目录）。这样：不进 git、不进便携包；
用户重装应用或换便携包时数据目录是保留的，下过的字体不用重下；而且便携包可能落在
只读位置，写数据目录才稳。查找时**先看下载目录、再看内置目录**，所以想换同名字体只要
下载覆盖即可。

上游来源与授权：`licenses/NotoCJK-OFL.txt`（思源黑体/宋体，Adobe + Google）、
`licenses/LXGWWenKai-OFL.txt`（霞鹜文楷，另含其上游 Klee 项目的版权行）、
`licenses/SmileySans-LICENSE.txt`（得意黑）。重下时用上表的 sha256 核对，别只信文件名。

## 四条不许踩的规矩（都是实测踩出来的）

1. **`Fontname` 必须逐字匹配字体内部的 family name，不能写文件名。** 写错时 libass 会
   **静默回落系统字体**（中文直接变豆腐块），而 ffmpeg 不报错、退出码是 0。
   实测活样本：得意黑的族名是 `Smiley Sans Oblique`，写成 `Smiley Sans` 时 libass
   回落到 `ArialMT` / `MicrosoftYaHeiUI`。加字体时**必须**跑一遍
   `probe_font_families.py`，把族名从文件里读出来。
2. **不用可变字体（variable font）。** libass 0.17 不支持 OpenType 可变字体的实例选择
   （带 `Bold` 位也只会渲染成 Regular）；Adobe 还警告 CFF2 可变字体在部分 Windows 10/11
   上会导致文字损坏。所以这里全部是**静态实例**。
3. **渲染时不要给 libass 绝对路径。** filtergraph 有两层转义，绝对路径要写成
   `C\\:/path`（**两级**反斜杠）才对；实测按文档那样写一级转义是**失败**的。
   做法是：把这次要用到的字体**硬链接**进本次渲染的工作目录，然后 `fontsdir=.`——
   同卷零拷贝（实测 0.0 ms），也完全不碰转义。
4. **只挂用到的那几份。** libass 会把 `fontsdir` 里的字体**全量解析**：
   实测整库（80 MB）0.16 s、单份 0.08 s。逐镜渲染时这个差价会累积，所以由
   `subtitles.fonts_used()` 给出这一款版式真正需要的那一份，按需挂。

注：OFL 带「保留字体名」条款。本项目**只做再分发、不改字体文件、不改名**，
所以这一条不受影响；如果以后要做子集化或改名，必须先处理这条。
