"""Importing skill_metrics must not pull SciPy/statsmodels into the process.

Worker and verifier processes import skill_execute -> skill_metrics at start-up;
the statistics libraries are only needed when an evaluation is scored.
"""

from __future__ import annotations

import json
import subprocess
import sys


def test_importing_skill_metrics_does_not_load_scipy_or_statsmodels() -> None:
    code = (
        "import json, sys\n"
        "import aether_worker.skill_metrics\n"
        "heavy = sorted(name for name in sys.modules if name.split('.')[0] in {'scipy', 'statsmodels'})\n"
        "print(json.dumps(heavy))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert loaded == [], loaded[:20]
