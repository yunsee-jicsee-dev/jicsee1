#!/usr/bin/env python3
"""현재 OS/CPU용 단일 실행 파일을 PyInstaller 로 만든다.

    python -m pip install -r requirements-build.txt
    python build.py

결과: dist/orico-nvme-editor-<windows|linux>-<x64|arm64>[.exe]
PyInstaller 는 교차 빌드를 지원하지 않으므로 각 OS/CPU 에서 따로 실행해야 한다
(GitHub Actions 워크플로가 4가지 조합을 모두 빌드한다).
"""
import os
import platform
import subprocess
import sys

ARCH_NAMES = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def target_name() -> str:
    arch = platform.machine().lower()
    arch = ARCH_NAMES.get(arch, arch)
    if sys.platform == "win32":
        os_name = "windows"
    elif sys.platform.startswith("linux"):
        os_name = "linux"
    else:
        os_name = sys.platform
    return f"orico-nvme-editor-{os_name}-{arch}"


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from nvme_hexedit import _fix_stdio
    _fix_stdio()  # Windows CI 처럼 출력이 cp1252 이어도 한글 메시지에서 죽지 않게
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("경고: 이 Python 에 tkinter 가 없어 GUI 없이(CLI 전용) 빌드됩니다.", file=sys.stderr)
    name = target_name()
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--onefile", "--clean", "--noconfirm",
                           "--name", name, "nvme_hexedit.py"], cwd=here)
    print(f"완료: {os.path.join(here, 'dist', name)}{'.exe' if sys.platform == 'win32' else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
