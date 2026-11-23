"""Host-side paths for the admitted worker deployment."""
import json
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]


def native_cli():
    runtime = ROOT / "src/Factory.Runtime"
    arch = platform.machine().lower()
    key = sys.platform + "-" + ("x64" if arch in ("amd64", "x86_64") else arch)
    paths = json.loads((runtime / "native-cli.json").read_text(encoding="utf-8"))
    if key not in paths:
        raise RuntimeError("Unsupported pinned Codex platform: " + key)
    return runtime / "node_modules" / paths[key]


def controller_directory():
    configured = os.getenv("FACTORY_CONTROLLER_RELEASE")
    if configured is None:
        return ROOT / "src/Factory.Api/bin/Debug/net10.0"
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("Controller release directory must be absolute")
    return path


def controller_command():
    directory = controller_directory()
    if "FACTORY_CONTROLLER_RELEASE" in os.environ:
        return [directory / ("Factory.Api.exe" if os.name == "nt" else "Factory.Api")]
    return ["dotnet", directory / "Factory.Api.dll"]
