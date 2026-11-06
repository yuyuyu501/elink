"""依次运行全部回归测试（测试文件自带 ``main()``，不依赖 pytest）。

打包脚本调用它：任一套失败就以非零退出，避免把坏包发出去。

注意：测试模块内部用 ``sys.exit(main())`` 表示结果，直接 ``runpy`` 会让第一个
模块的 ``SystemExit`` 把后面几套一起带走，所以这里逐个捕获。
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "tests/test_mpegts_roundtrip.py",
    "tests/test_protocols.py",
    "tests/test_audio_and_session.py",
)


def run(suites: tuple[str, ...] = SUITES) -> int:
    failures: list[str] = []
    for name in suites:
        path = ROOT / name
        if not path.is_file():
            print(f"[SKIP] {name} 不存在")
            continue
        print(f"[RUN ] {name}", flush=True)
        try:
            runpy.run_path(str(path), run_name="__main__")
        except SystemExit as exc:
            if exc.code:
                failures.append(name)
                print(f"[FAIL] {name} (exit={exc.code})", flush=True)
            else:
                print(f"[ OK ] {name}", flush=True)
        except Exception as exc:  # 模块级异常同样算失败
            failures.append(name)
            print(f"[ERR ] {name}: {type(exc).__name__}: {exc}", flush=True)

    print(f"\n{len(failures)} suite(s) failed")
    for name in failures:
        print(f" - {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
