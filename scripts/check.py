"""依次运行全部回归测试（测试文件自带 ``main()``，不依赖 pytest）。

打包脚本调用它：任一套失败就以非零退出，避免把坏包发出去。

每组测试使用独立进程，避免 Qt 应用和后台线程跨组共享生命周期。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "tests/test_mpegts_roundtrip.py",
    "tests/test_protocols.py",
    "tests/test_audio_and_session.py",
    "tests/test_device_ui.py",
    "tests/test_network_discovery.py",
    "tests/test_desktop_connection.py",
    "tests/test_remote_input.py",
    "tests/test_stream_latency.py",
    "tests/test_codec_selection.py",
    "tests/test_error_report.py",
    "tests/test_packaging.py",
)


def run(suites: tuple[str, ...] = SUITES) -> int:
    failures: list[str] = []
    for name in suites:
        path = ROOT / name
        if not path.is_file():
            print(f"[SKIP] {name} 不存在")
            continue
        print(f"[RUN ] {name}", flush=True)
        result = subprocess.run([sys.executable, str(path)], cwd=ROOT)
        if result.returncode:
            failures.append(name)
            print(f"[FAIL] {name} (exit={result.returncode})", flush=True)
        else:
            print(f"[ OK ] {name}", flush=True)

    print(f"\n{len(failures)} suite(s) failed")
    for name in failures:
        print(f" - {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
