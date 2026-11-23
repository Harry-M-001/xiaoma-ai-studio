"""桌面启动器的纯逻辑：端口、数据目录、健康检查地址。不开窗口。"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "desktop" / "launcher.py"


def _load():
    spec = importlib.util.spec_from_file_location("xiaoma_desktop_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["xiaoma_desktop_launcher"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_free_port_is_bindable_and_health_url_uses_it():
    mod = _load()
    port = mod.find_free_port()
    assert 0 < port < 65536
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
    assert mod.health_url(port).endswith(f":{port}/api/health")


def test_data_dir_defaults_to_appdata_and_portable_follows_program_dir(tmp_path):
    mod = _load()
    default = mod.resolve_data_dir(tmp_path, portable=False)
    assert default.name == "data" and default.parent.name == "XiaomaAIStudio"
    portable = mod.resolve_data_dir(tmp_path, portable=True)
    assert portable == tmp_path / "backend" / "data"
    override = mod.resolve_data_dir(tmp_path, portable=True, override=str(tmp_path / "x"))
    assert override == (tmp_path / "x").resolve()


def test_source_layout_prefers_project_venv():
    mod = _load()
    base, python = mod.resolve_layout()
    assert base == ROOT
    venv = ROOT / "backend" / "venv" / "Scripts" / "python.exe"
    if venv.is_file():
        assert python == venv
