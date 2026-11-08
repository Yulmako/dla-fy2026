#!/usr/bin/env python3
"""Full FY2026 refresh from the public API: probe (lag status) -> staging -> load.

    python scripts/refresh_fy2026.py

Stops at the first failing step; the previous snapshot stays live. The load step
refuses to run while the duplicate treatment is 'undecided'.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = ["phase1_api_probe.py", "phase2a_stage_and_profile.py", "load_fy2026.py"]


def main() -> int:
    for step in STEPS:
        print(f"== {step}")
        rc = subprocess.call([sys.executable, str(ROOT / "scripts" / step)], cwd=str(ROOT))
        if rc != 0:
            print(f"{step} exited with {rc}; refresh stopped, previous snapshot unchanged.")
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
