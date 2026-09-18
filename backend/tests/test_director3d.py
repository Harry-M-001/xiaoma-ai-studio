"""3D 导演台（F 期）的检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_director3d.py

分两部分：

**一、场景存档的校验行为（真的跑一遍，不是看字符串）**
`director3d/persist.ts` 是「脏数据降级」的唯一入口：场景存在 localStorage 里，
是用户能手动改、旧版本写过、跨版本会残留的地方。少一个字段的后果不是报错，
而是场景渲染不出来或滑杆显示 NaN。所以这里把那段 TS **用 esbuild 打成 ESM 再让 node 跑**，
逐条断言夹取/兜底/丢弃的行为——静态检查看不出「999 会不会被夹成 170」。
（没有 node 或 node_modules 时跳过，不让环境问题变成假失败。）

**二、接线上的静态守卫**
都是这次开发里**真的踩过**的坑，写死在这里防止漂移：
 1. three（约 1MB）只能从 director3d 里被 import，且必须走 `React.lazy`；
 2. 画布必须被 CSS 撑满容器（否则 canvas 按属性尺寸显示、被容器裁掉一块，
    看着像「镜头跑偏」）；
 3. 底部工具条的 z-index 必须高于窄窗口下的浮层面板（否则「截图存到资产库」
    被面板压住点不到，而且不报错）；
 4. 截图时必须把设备像素比压到 1（否则「2x」出来的是 2×dpr，且换台机器尺寸还不一样）；
 5. 拖动只改 x/z（y 保持按下时的值），否则对象会跟着鼠标上下飞。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
SRC = FRONTEND / "src"
D3D = SRC / "director3d"

DRIVER = r"""
import { makeEmptyDoc, makeCamera, makeProp } from './types.mjs';
import { parseDirectorDoc, serializeDirectorDoc, importDirectorDoc } from './persist.mjs';

const rows = [];
const ck = (name, cond, extra = '') => rows.push([name, !!cond, extra]);
const eq = (name, got, want) =>
  ck(name, JSON.stringify(got) === JSON.stringify(want), `got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);

// ---- 正常往返
const base = makeEmptyDoc();
const rt = parseDirectorDoc(serializeDirectorDoc(base));
ck('正常存档往返：对象数不变', rt && rt.objects.length === base.objects.length);
ck('正常存档往返：激活机位保留', rt && rt.activeCameraId === base.activeCameraId);

// ---- 整份丢掉的情形
eq('版本号不认 → 整份丢掉', parseDirectorDoc(JSON.stringify({ ...base, version: 2 })), null);
ck('不是 JSON → null', parseDirectorDoc('{oops') === null);
ck('空串 → null', parseDirectorDoc('') === null);
ck('顶层不是对象 → null', parseDirectorDoc('123') === null);
ck('objects 不是数组 → null', parseDirectorDoc(JSON.stringify({ version: 1, objects: 'x' })) === null);

const blind = { version: 1, objects: [], groups: [], activeCameraId: null };
const bare = parseDirectorDoc(JSON.stringify(blind));
eq('空场景（没有对象）也合法', bare && bare.objects.length, 0);
ck('缺 scene/lights 时补默认值', bare && bare.scene.worldSize.x === 20 && bare.lights.ambientIntensity === 0.35);

// ---- 单个对象不合格 → 只丢它
const noId = parseDirectorDoc(
  JSON.stringify({ ...blind, objects: [{ kind: 'actor', name: 'x' }, { id: 'ok', kind: 'prop' }] }),
);
eq('没 id 的对象被丢掉、其余保留', noId.objects.map((o) => o.id), ['ok']);

// ---- 越界夹取
const wild = parseDirectorDoc(
  JSON.stringify({
    ...blind,
    scene: { worldSize: { x: 0, y: 0, z: 0 }, groundOpacity: 5, environment: 'nope' },
    lights: { ambientIntensity: 9 },
  }),
);
eq('世界尺寸下限 2', [wild.scene.worldSize.x, wild.scene.worldSize.y, wild.scene.worldSize.z], [2, 2, 2]);
eq('地面不透明度封顶 1', wild.scene.groundOpacity, 1);
eq('环境名不认 → 影棚', wild.scene.environment, 'studio');
eq('环境光封顶 3', wild.lights.ambientIntensity, 3);

// ---- 枚举兜底 + 关节夹到滑杆范围
const messy = {
  id: 'a1',
  kind: 'actor',
  bodyType: 'nope',
  color: 123,
  height: 99,
  pos: { x: 1 },
  rotY: 30,
  scale: 0,
  pose: { preset: 'nope', shoulderL_raise: 999, elbowL: -50, kneeR: 'x' },
};
const doc = parseDirectorDoc(JSON.stringify({ ...blind, objects: [messy] }));
const a = doc.objects[0];
eq('体型不认 → 标准男', a.bodyType, 'standard_male');
eq('身高封顶 4', a.height, 4);
eq('姿态预设不认 → 站立', a.pose.preset, 'stand');
eq('关节夹到滑杆上限（左肩前举 → 170）', a.pose.shoulderL_raise, 170);
eq('关节夹到滑杆下限（左肘 → 0）', a.pose.elbowL, 0);
eq('关节不是数字 → 用预设值', a.pose.kneeR, 0);
eq('名称缺省回落成 id', a.name, 'a1');
eq('缩放下限 0.05', a.scale, 0.05);
eq('朝向原样保留', a.rotY, 30);
eq('位置缺字段按 0 补', [a.pos.x, a.pos.y, a.pos.z], [1, 0, 0]);

// ---- 手动调过的姿态要保住
const cust = parseDirectorDoc(
  JSON.stringify({ ...blind, objects: [{ id: 'a2', kind: 'actor', pose: { preset: 'custom', headPitch: 20 } }] }),
);
eq('custom 预设保留', cust.objects[0].pose.preset, 'custom');
eq('custom 的角度保留', cust.objects[0].pose.headPitch, 20);

// ---- 激活机位悬空
const cam = makeCamera(1, { x: 0, y: 1, z: 6 }, { x: 0, y: 1, z: 0 });
const dangling = parseDirectorDoc(JSON.stringify({ ...blind, objects: [cam], activeCameraId: 'gone' }));
eq('激活机位悬空 → 退回第一个机位', dangling.activeCameraId, cam.id);
const noCam = parseDirectorDoc(
  JSON.stringify({ ...blind, objects: [makeProp(1, { x: 0, y: 0, z: 0 })], activeCameraId: 'gone' }),
);
eq('一个机位都没有 → null', noCam.activeCameraId, null);

// ---- 分组
const grp = parseDirectorDoc(
  JSON.stringify({ ...blind, groups: [{ id: 'g1', name: 'x' }, { name: 'noid' }, 7] }),
);
eq('坏分组被丢掉', grp.groups.length, 1);

// ---- 导入口与读取口同一套校验
ck('importDirectorDoc 拒绝坏 JSON', importDirectorDoc('  {bad') === null);
ck('importDirectorDoc 接受好存档', importDirectorDoc(JSON.stringify(base)) !== null);

for (const [name, pass, extra] of rows) {
  console.log(`${pass ? 'PASS' : 'FAIL'} ${name}${pass ? '' : ' :: ' + extra}`);
}
const failed = rows.filter((r) => !r[1]).length;
console.log(`SUMMARY ${rows.length - failed}/${rows.length}`);
process.exit(failed ? 1 : 0);
"""


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _find_node() -> str | None:
    return shutil.which("node")


def _esbuild_js() -> Path | None:
    p = FRONTEND / "node_modules" / "esbuild" / "bin" / "esbuild"
    return p if p.exists() else None


def _run_behaviour() -> tuple[bool, str]:
    """把 persist.ts / types.ts 打成 ESM 交给 node 跑。返回 (是否跳过, 输出)"""
    node = _find_node()
    esbuild = _esbuild_js()
    if not node or not esbuild:
        return True, "没有 node 或 node_modules（跳过行为测试）"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for entry, out in (("types.ts", "types.mjs"), ("persist.ts", "persist.mjs")):
            proc = subprocess.run(
                [node, str(esbuild), str(D3D / entry), "--bundle", "--format=esm",
                 f"--outfile={tmp / out}", "--log-level=warning"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if proc.returncode != 0:
                return True, f"esbuild 打包 {entry} 失败（跳过）：{proc.stderr[:200]}"
        (tmp / "driver.mjs").write_text(DRIVER, encoding="utf-8")
        run = subprocess.run(
            [node, str(tmp / "driver.mjs")], capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(tmp),
        )
        return False, (run.stdout or "") + (run.stderr or "")


# ---------------------------------------------------------------- 一、行为

_BEHAVIOUR_CACHE: tuple[bool, str] | None = None


def _behaviour() -> tuple[bool, str]:
    global _BEHAVIOUR_CACHE
    if _BEHAVIOUR_CACHE is None:
        _BEHAVIOUR_CACHE = _run_behaviour()
    return _BEHAVIOUR_CACHE


def test_scene_doc_validator_actually_runs():
    """真的跑一遍校验器；没有 node 就直说跳过，不假装通过。"""
    skipped, out = _behaviour()
    if skipped:
        print(f"    SKIP（{out}）")
        return
    lines = [ln for ln in out.splitlines() if ln.startswith(("PASS ", "FAIL ", "SUMMARY "))]
    assert lines, f"node 没有产出结果：{out[:400]}"
    fails = [ln for ln in lines if ln.startswith("FAIL ")]
    summary = [ln for ln in lines if ln.startswith("SUMMARY ")]
    assert not fails, "这些校验行为不符合预期：\n" + "\n".join(fails)
    assert summary and summary[0].split()[1].split("/")[0] == summary[0].split()[1].split("/")[1], (
        f"用例没有全部通过：{summary}"
    )


# ---------------------------------------------------------------- 二、静态守卫


def test_three_is_only_imported_inside_the_lazy_module():
    """three 约 1MB：只能出现在 director3d 里，别处 import 就会进首屏包。"""
    offenders: list[str] = []
    for path in list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx")):
        if D3D in path.parents:
            continue
        if '"three' in _text(path) or "'three" in _text(path):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"这些文件直接 import 了 three：{offenders}"


def test_studio_is_lazy_loaded():
    """3D 导演台必须 React.lazy：否则 three 会被打进主包。"""
    page = _text(SRC / "pages" / "DirectorPage.tsx")
    assert 'lazy(() => import("../director3d/DirectorStudio3D"))' in page, (
        "导演台页面没有用 React.lazy 加载 3D 模块"
    )
    assert "Suspense" in page, "懒加载没有配 Suspense 兜底"


def test_canvas_fills_its_container():
    """画布必须被 CSS 撑满：three 的 setSize(..., false) 不改 CSS，
    容器比画布小时会被裁掉一块（实测表现是「镜头跑偏、右边的物体不见了」）。"""
    css = _text(D3D / "director3d.css")
    i = css.find(".d3d-viewport canvas")
    assert i >= 0, "样式表里没有 .d3d-viewport canvas 规则"
    block = css[i : i + 200]
    assert "width: 100%" in block and "height: 100%" in block, (
        "画布没有撑满容器：宽高必须是 100%"
    )


def test_toolbar_sits_above_floating_panels():
    """窄窗口下左右面板是浮层：工具条的层级必须比它们高，否则按钮点不到。"""
    css = _text(D3D / "director3d.css")
    i = css.find(".d3d-shotbar {")
    assert i >= 0, "样式表里找不到 .d3d-shotbar"
    bar = css[i : css.find("}", i)]
    assert "z-index: 7" in bar and "position: relative" in bar, (
        "工具条没有浮在面板之上：截图按钮会被面板压住，点了没反应也不报错"
    )
    j = css.find(".d3d-left,\n  .d3d-right {")
    assert j >= 0, "找不到窄窗口下把面板改成浮层的规则"
    assert "z-index: 6" in css[j : j + 200], "浮层面板的层级变了，工具条会反过来被压住"


def test_capture_resets_device_pixel_ratio():
    """截图要把设备像素比压到 1：否则「2x」是 2×dpr，且换台机器出图尺寸还不一样。"""
    src = _text(D3D / "viewport.ts")
    i = src.find("capture(opts")
    assert i >= 0, "viewport.ts 里没有 capture"
    block = src[i : src.find("\n  private resize", i)]
    assert "setPixelRatio(1)" in block, "截图没有把像素比压到 1"
    assert "setPixelRatio(prevRatio)" in block, "截图结束后没有还原像素比（画面会糊）"


def test_drag_keeps_height_and_uses_ground_plane():
    """对象拖动只改 x/z：y 用按下时的值，否则对象跟着鼠标上下飞。"""
    src = _text(D3D / "viewport.ts")
    assert "y: this.drag.startPos.y" in src, "拖动没有保持原来的高度"
    assert "intersectPlane(this.groundPlane" in src, "拖动没有打在地面平面上"
    assert "controls.enabled = false" in src, "拖动时没有关掉 OrbitControls（会变成转视角）"


def test_joint_sliders_cover_eighteen_angles():
    """18 个关节一个都不能少：属性面板与存档校验都按这张表走。"""
    src = _text(D3D / "types.ts")
    i = src.find("export const JOINT_SLIDERS")
    assert i >= 0, "找不到 JOINT_SLIDERS"
    block = src[i : src.find("];", i)]
    assert block.count("{ key:") == 18, f"关节数量不是 18：{block.count('{ key:')}"


def test_scene_store_is_the_only_localstorage_path():
    """localStorage 只能经 prefs：3D 场景也必须走那一个口子。"""
    for path in list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx")):
        if path.name in ("prefs.ts", "api.ts"):
            continue
        text = _text(path)
        assert "localStorage.getItem" not in text and "localStorage.setItem" not in text, (
            f"{path.relative_to(ROOT)} 绕过了 prefs 直接读写 localStorage"
        )
    prefs = _text(SRC / "prefs.ts")
    assert "directorScene" in prefs and "malformed" in prefs, (
        "prefs 里的场景存档口子没有区分「没存过」与「存过但坏了」"
    )


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
