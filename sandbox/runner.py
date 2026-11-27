"""Executes the cell (default /qd/cell.py, read-only control mount), then saves any open matplotlib figures to /work/out/.

Runs inside the sandbox container. Output protocol: normal stdout/stderr from the cell,
then a final line `__QD_FIGURES__ <json list of saved paths>`.
"""
import json
import os
import runpy
import shutil
import sys
import traceback

CELL = sys.argv[1] if len(sys.argv) > 1 else "/qd/cell.py"

os.makedirs("/tmp/mpl", exist_ok=True)
if os.path.isdir("/opt/mpl"):
    shutil.copytree("/opt/mpl", "/tmp/mpl", dirs_exist_ok=True)
os.makedirs("/work/out", exist_ok=True)

# Track figures the cell saved itself, so they are not exported a second time below.
_explicitly_saved = set()
try:
    import matplotlib.figure as _mf
    _orig_savefig = _mf.Figure.savefig

    def _tracking_savefig(self, *args, **kwargs):
        _explicitly_saved.add(id(self))
        return _orig_savefig(self, *args, **kwargs)

    _mf.Figure.savefig = _tracking_savefig
except ImportError:
    pass

status = 0
try:
    runpy.run_path(CELL, run_name="__main__")
except SystemExit as e:
    status = e.code if isinstance(e.code, int) else 0
except BaseException:
    traceback.print_exc()
    status = 1

saved = []
try:
    import matplotlib.pyplot as plt
    existing = len([f for f in os.listdir("/work/out") if f.startswith("figure_")])
    for i, num in enumerate(plt.get_fignums(), start=existing + 1):
        fig = plt.figure(num)
        if not fig.axes or id(fig) in _explicitly_saved:
            continue
        path = f"/work/out/figure_{i}.png"
        fig.savefig(path, dpi=130, bbox_inches="tight")
        saved.append(path)
    plt.close("all")
except ImportError:
    pass
except BaseException:
    traceback.print_exc()

sys.stdout.flush()
print("\n__QD_FIGURES__ " + json.dumps(saved))
sys.exit(status)
