"""一条命令发版：预检 → 推 tag → 双平台 Release → 上传 → 回下载校验。

它只做「发版后半段」：版本号、CHANGELOG、提交由人负责（预检会检查一致性），
打包可以加 `--build` 交给 make_portable.py。

用法：
    backend\\venv\\Scripts\\python.exe backend\\tools\\release.py            # 用当前版本号发版
    backend\\venv\\Scripts\\python.exe backend\\tools\\release.py --build    # 先打包再发
    backend\\venv\\Scripts\\python.exe backend\\tools\\release.py --dry-run  # 只做预检

凭证：
- Gitee：从 `git credential fill` 取（与普通 git push 同一份，不落仓库）
- GitHub：读环境变量 `GH_TOKEN`

为什么值得做成脚本：Gitee 有 1 GB 附件配额，满了要「删最老几个 Release 再重建」；
而重建会让它变成「最新」，必须再把本次 Release 重建一次把顺序修回来。
这套顺序手工做错过两次，写进代码只需要错一次。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
INIT = BACKEND / "app" / "__init__.py"
PKG = ROOT / "frontend" / "package.json"
CHANGELOG = ROOT / "CHANGELOG.md"
DIST = ROOT / "dist"
VENV_PY = BACKEND / "venv" / "Scripts" / "python.exe"

GH_OWNER, GH_REPO = "Harry-M-001", "xiaoma-ai-studio"
GT_OWNER, GT_REPO = "haoruiM", "xiaoma-ai-studio"
GT_BASE = f"https://gitee.com/api/v5/repos/{GT_OWNER}/{GT_REPO}"
GH_BASE = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}"

UA = "xiaoma-release"
ZIP_SUFFIX = "win64-portable.zip"
# 配额不足时一次清几个：太少要反复试，太多会白删还能用的旧包
PRUNE_BATCH = 5
MAX_PRUNE = 15


# --------------------------------------------------------------------------
# 预检
# --------------------------------------------------------------------------


def read_version() -> tuple[str, str]:
    """返回 (后端版本, 前端 package.json 版本)。"""
    init_text = INIT.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    if not m:
        raise SystemExit(f"{INIT} 里找不到 __version__")
    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    return m.group(1), str(pkg.get("version") or "")


def changelog_section(tag: str) -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.find(f"## {tag}")
    if start < 0:
        raise SystemExit(f"CHANGELOG 里没有 `## {tag}` 段落，先补发版说明")
    end = text.find("\n## ", start + 3)
    section = text[start : end if end != -1 else len(text)].strip()
    body = section.split("\n", 1)[1].strip() if "\n" in section else ""
    if not body:
        raise SystemExit(f"`## {tag}` 下面是空的，先写这版改了什么")
    return body


def git(*args: str, check: bool = True, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def preflight(tag: str) -> None:
    back, front = read_version()
    if back != front:
        raise SystemExit(f"版本号不一致：backend={back} frontend={front}")
    if tag != f"v{back}":
        raise SystemExit(f"tag {tag} 与本地版本 v{back} 不一致")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise SystemExit(f"当前分支是 {branch}，发版请切到 main")
    # 只看已跟踪文件：未跟踪的截图 / 临时文件不该拦住发版
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise SystemExit(f"还有未提交的改动，先提交再发版：\n{dirty}")
    changelog_section(tag)  # 顺带校验发版说明非空
    print(f"预检通过：v{back}（{branch}，工作区干净）")


def ensure_tag(tag: str) -> None:
    if git("tag", "-l", tag):
        head = git("rev-parse", "HEAD")
        tagged = git("rev-list", "-n", "1", tag)
        if head != tagged:
            raise SystemExit(f"tag {tag} 已存在但不指向 HEAD（{tagged[:8]} ≠ {head[:8]}）")
        print(f"tag {tag} 已存在，跳过创建")
        return
    git("tag", tag)
    print(f"已创建 tag {tag}")


def push_all(tag: str) -> None:
    for remote in ("gitee", "origin"):
        print(f"推送 {remote} main + {tag} …")
        git("push", remote, "main", timeout=600)
        git("push", remote, tag, timeout=600)


# --------------------------------------------------------------------------
# 网络小工具
# --------------------------------------------------------------------------


def call(req: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def json_call(url: str, method: str, payload: Any = None, headers: dict | None = None):
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    status, raw = call(urllib.request.Request(url, data=data, headers=hdrs, method=method))
    try:
        return status, json.loads(raw or b"null")
    except json.JSONDecodeError:
        return status, raw.decode("utf-8", "replace")


def gitee_token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=gitee.com\n\n",
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise SystemExit("取不到 Gitee token：确认 git push gitee 用过且凭证还在")


def upload_multipart(url: str, path: Path) -> tuple[int, bytes]:
    boundary = "----xiaoma" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'
    ).encode()
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return call(urllib.request.Request(
        url, data=body,
        headers={"User-Agent": UA, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    ))


def zip_asset(rel: dict) -> dict | None:
    for a in rel.get("assets") or []:
        if str(a.get("name") or "").endswith(ZIP_SUFFIX):
            return a
    return None


def pick_prune_candidates(releases: list[dict], keep_tag: str) -> list[dict]:
    """挑出「最老、且还挂着便携包」的 Release；不含本次要发的那个。"""
    out = [
        r for r in releases
        if r.get("tag_name") != keep_tag and zip_asset(r) is not None
    ]
    return sorted(out, key=lambda r: int(r.get("id") or 0))


# --------------------------------------------------------------------------
# Gitee
# --------------------------------------------------------------------------


class Gitee:
    def __init__(self, token: str):
        self.t = token

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{GT_BASE}{path}{sep}access_token={self.t}"

    def get(self, path: str):
        return json_call(self._url(path), "GET")

    def post(self, path: str, payload: dict):
        return json_call(self._url(path), "POST", payload)

    def delete(self, path: str):
        return json_call(self._url(path), "DELETE")

    def releases(self) -> list[dict]:
        _, data = self.get("/releases?per_page=100")
        return data if isinstance(data, list) else []

    def ensure_release(self, tag: str, name: str, body: str) -> dict:
        status, rel = self.get(f"/releases/tags/{tag}")
        if isinstance(rel, dict) and rel.get("id"):
            print(f"Gitee Release 已存在（id={rel['id']}），复用")
            return rel
        status, rel = self.post("/releases", {
            "tag_name": tag, "name": name, "body": body, "target_commitish": "main",
        })
        if status not in (200, 201) or not isinstance(rel, dict) or not rel.get("id"):
            raise SystemExit(f"Gitee 建 Release 失败 {status}: {rel}")
        print(f"Gitee Release 已创建（id={rel['id']}）")
        return rel

    def upload(self, rel: dict, path: Path) -> None:
        if zip_asset(rel) is not None:
            print("Gitee 已有同名便携包，跳过上传")
            return
        status, raw = upload_multipart(
            self._url(f"/releases/{rel['id']}/attach_files"), path
        )
        if status in (200, 201):
            print("Gitee 便携包已上传")
            return
        raise _QuotaError(raw.decode("utf-8", "replace"))

    def prune_one(self, rel: dict) -> None:
        """删掉一个旧 Release 再按原样重建：只丢 zip，tag 与说明都保留。"""
        tag = rel["tag_name"]
        status, detail = self.get(f"/releases/tags/{tag}")
        body = detail.get("body") if isinstance(detail, dict) else None
        name = detail.get("name") if isinstance(detail, dict) else tag
        status, _ = self.delete(f"/releases/{rel['id']}")
        if status not in (200, 204):
            raise SystemExit(f"删除旧 Release {tag} 失败：{status}")
        status, new = self.post("/releases", {
            "tag_name": tag, "name": name or tag, "body": body or "",
            "target_commitish": tag,
        })
        ok = isinstance(new, dict) and new.get("id")
        print(f"  已清理 {tag} 的便携包（Release 重建 {'成功' if ok else '失败'}）")

    def ensure_latest(self, tag: str, name: str, body: str, path: Path) -> None:
        """重建旧 Release 会把它顶到「最新」，这里把本次 Release 重建回来。"""
        _, latest = self.get("/releases/latest")
        if isinstance(latest, dict) and latest.get("tag_name") == tag:
            return
        print("最新 Release 顺序被清理动作打乱，重建本次 Release …")
        status, rel = self.get(f"/releases/tags/{tag}")
        if isinstance(rel, dict) and rel.get("id"):
            self.delete(f"/releases/{rel['id']}")
        status, rel = self.post("/releases", {
            "tag_name": tag, "name": name, "body": body, "target_commitish": "main",
        })
        if status not in (200, 201) or not isinstance(rel, dict) or not rel.get("id"):
            raise SystemExit(f"重建本次 Release 失败 {status}: {rel}")
        self.upload(rel, path)
        _, latest2 = self.get("/releases/latest")
        if not isinstance(latest2, dict) or latest2.get("tag_name") != tag:
            raise SystemExit("重建后 latest 仍不是本次版本，请手工检查 Gitee")
        print("Gitee latest 已修正")


class _QuotaError(Exception):
    pass


def publish_gitee(tag: str, name: str, body: str, zip_path: Path) -> dict:
    gt = Gitee(gitee_token())
    rel = gt.ensure_release(tag, name, body)
    try:
        gt.upload(rel, zip_path)
    except _QuotaError as e:
        text = str(e)
        if "配额" not in text:
            raise SystemExit(f"Gitee 上传失败：{text[:300]}")
        print("Gitee 附件配额不足，开始清理最老的便携包 …")
        pruned = 0
        while pruned < MAX_PRUNE:
            candidates = pick_prune_candidates(gt.releases(), tag)
            batch = candidates[:PRUNE_BATCH]
            if not batch:
                raise SystemExit("没有可清理的旧便携包了，但配额仍不足")
            for old in batch:
                gt.prune_one(old)
                pruned += 1
            try:
                gt.upload(rel, zip_path)
                break
            except _QuotaError as again:
                if "配额" not in str(again):
                    raise SystemExit(f"Gitee 上传失败：{str(again)[:300]}")
                print("  仍然不足，继续清理下一批 …")
        else:
            raise SystemExit("清理到上限仍上传失败，请手工检查 Gitee 配额")
    gt.ensure_latest(tag, name, body, zip_path)
    return {"id": rel["id"]}


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def publish_github(tag: str, name: str, body: str, zip_path: Path) -> dict:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise SystemExit("没有 GH_TOKEN 环境变量，无法发 GitHub Release")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    status, rel = json_call(f"{GH_BASE}/releases/tags/{tag}", "GET", headers=headers)
    if not (isinstance(rel, dict) and rel.get("id")):
        status, rel = json_call(f"{GH_BASE}/releases", "POST", {
            "tag_name": tag, "name": name, "body": body, "draft": False, "prerelease": False,
        }, headers=headers)
        if status != 201:
            raise SystemExit(f"GitHub 建 Release 失败 {status}: {rel}")
        print("GitHub Release 已创建")
    else:
        print("GitHub Release 已存在，复用")

    for a in rel.get("assets") or []:
        if a.get("name") == zip_path.name:
            json_call(f"{GH_BASE}/releases/assets/{a['id']}", "DELETE", headers=headers)
            print("GitHub 旧同名资产已删除")

    upload_base = rel["upload_url"].split("{", 1)[0]
    status, raw = call(urllib.request.Request(
        f"{upload_base}?name={urllib.parse.quote(zip_path.name)}",
        data=zip_path.read_bytes(),
        headers={**headers, "Content-Type": "application/zip"},
        method="POST",
    ))
    if status not in (200, 201):
        raise SystemExit(f"GitHub 上传失败 {status}: {raw[:300]}")
    print("GitHub 便携包已上传")
    return rel


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dst: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=900) as r, dst.open("wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def verify(tag: str, zip_path: Path) -> None:
    token = gitee_token()
    _, gt = json_call(f"{GT_BASE}/releases/latest?access_token={token}", "GET")
    if not (isinstance(gt, dict) and gt.get("tag_name") == tag):
        raise SystemExit(f"Gitee latest 不是 {tag}：{gt.get('tag_name') if isinstance(gt, dict) else gt}")
    gt_asset = zip_asset(gt)
    if gt_asset is None:
        raise SystemExit("Gitee latest 上没有便携包")

    gh_headers = {
        "Authorization": f"Bearer {os.environ.get('GH_TOKEN','')}",
        "Accept": "application/vnd.github+json",
    }
    _, gh = json_call(f"{GH_BASE}/releases/latest", "GET", headers=gh_headers)
    if not (isinstance(gh, dict) and gh.get("tag_name") == tag):
        raise SystemExit(f"GitHub latest 不是 {tag}")
    gh_asset = zip_asset(gh)
    if gh_asset is None:
        raise SystemExit("GitHub latest 上没有便携包")

    local = sha256_file(zip_path)
    results: dict[str, tuple[int, str]] = {}

    def fetch(name: str, url: str) -> None:
        tmp = Path(tempfile.gettempdir()) / f"verify-{name}-{tag}.zip"
        download(url, tmp)
        results[name] = (tmp.stat().st_size, sha256_file(tmp))
        tmp.unlink(missing_ok=True)

    threads = [
        threading.Thread(target=fetch, args=("gitee", gt_asset["browser_download_url"])),
        threading.Thread(target=fetch, args=("github", gh_asset["browser_download_url"])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = True
    for name, (size, digest) in sorted(results.items()):
        match = digest == local and size == zip_path.stat().st_size
        ok = ok and match
        print(f"  {name}: {size} bytes, sha256 {'一致' if match else '不一致！'}")
    if not ok:
        raise SystemExit("回下载校验失败：附件与本地包不一致")
    print(f"校验通过：{local[:12]}…")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def build_portable() -> None:
    print("打包便携包（--skip-deps）…")
    subprocess.run(
        [str(VENV_PY), str(BACKEND / "tools" / "make_portable.py"), "--skip-deps"],
        cwd=str(BACKEND), check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="一条命令发版（预检 + 双平台 Release）")
    parser.add_argument("--build", action="store_true", help="发版前先打便携包")
    parser.add_argument("--dry-run", action="store_true", help="只做预检，不推送、不发 Release")
    parser.add_argument("--skip-verify", action="store_true", help="跳过回下载 sha256 校验")
    args = parser.parse_args()

    back, _ = read_version()
    tag = f"v{back}"
    zip_path = DIST / f"xiaoma-ai-studio-{tag}-win64-portable.zip"

    preflight(tag)
    if args.build:
        build_portable()
    if not zip_path.is_file():
        raise SystemExit(f"找不到便携包 {zip_path}，先跑 make_portable.py 或加 --build")
    print(f"便携包：{zip_path.name}（{zip_path.stat().st_size / 1048576:.1f} MB）")
    if args.dry_run:
        print("dry-run：预检完成，未做任何推送")
        return

    ensure_tag(tag)
    push_all(tag)

    name = f"{tag} · 小马AI工坊"
    body = changelog_section(tag)
    publish_gitee(tag, name, body, zip_path)
    publish_github(tag, name, body, zip_path)

    if not args.skip_verify:
        print("回下载校验两个附件 …")
        verify(tag, zip_path)

    print("\n发版完成：")
    print(f"  GitHub: https://github.com/{GH_OWNER}/{GH_REPO}/releases/tag/{tag}")
    print(f"  Gitee : https://gitee.com/{GT_OWNER}/{GT_REPO}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
