"""Paired, alternating before/after comparison of two checkouts of this project.

CPU speed on a PC drifts over time (boost clocks, background load), so measuring version A and then
version B is unreliable. This script alternates runs A, B, A, B, ... in fresh processes and also
records how fast the machine decoded video *during each run*, so drift is visible.

    python scripts/compare_versions.py --old PATH_TO_OLD_CHECKOUT --new . --pairs 6
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path


def run_once(tree: Path, quality: str, width: int, size: str, seconds: float,
             resolution: str) -> dict:
    script = Path(__file__).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "r.json"
        # Always use the *current* benchmark script, executed against the given tree.
        bench = tree / "scripts" / "benchmark.py"
        bench.parent.mkdir(exist_ok=True)
        bench.write_text((script.parent / "benchmark.py").read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run([sys.executable, "-u", str(bench), "--no-stages", "--e2e", "--quality", quality,
                        "--width", str(width), "--sizes", size, "--resolution", resolution, "--e2e-seconds", str(seconds),
                        "--json", str(out)], cwd=tree, check=True, capture_output=True)
        data = json.loads(out.read_text(encoding="utf-8"))
    run = next(iter(data["e2e"].values()))
    return {"cpu": run["cpu_percent_of_one_core"], "decode_ms": data["decode"]["median"],
            "sync_med": run["sync_error_ms_median"], "sync_p95": run["sync_error_ms_p95"],
            "skipped": run["frames_skipped"], "stalls": run["stalls"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--old", type=Path, required=True)
    ap.add_argument("--new", type=Path, default=Path("."))
    ap.add_argument("--pairs", type=int, default=6)
    ap.add_argument("--quality", default="high")
    ap.add_argument("--width", type=int, default=100)
    ap.add_argument("--size", default="160x45")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--resolution", default="1280x720", help="source video size")
    a = ap.parse_args()
    rows = {"old": [], "new": []}
    for i in range(a.pairs):
        for label, tree in (("old", a.old), ("new", a.new)):
            r = run_once(tree.resolve(), a.quality, a.width, a.size, a.seconds, a.resolution)
            rows[label].append(r)
            print(f"pair {i + 1}/{a.pairs} {label}: CPU {r['cpu']:.0f}% | machine decode {r['decode_ms']:.1f} ms/frame"
                  f" | sync {r['sync_med']:.0f}/{r['sync_p95']:.0f} ms | skipped {r['skipped']} stalls {r['stalls']}",
                  flush=True)
    print()
    for label in ("old", "new"):
        cpu = [r["cpu"] for r in rows[label]]
        print(f"{label}: CPU median {statistics.median(cpu):.0f}% (min {min(cpu):.0f}, max {max(cpu):.0f}); "
              f"decode speed during runs median {statistics.median(r['decode_ms'] for r in rows[label]):.1f} ms/frame; "
              f"sync error median {statistics.median(r['sync_med'] for r in rows[label]):.0f} ms")
    ratios = [n["cpu"] / o["cpu"] for o, n in zip(rows["old"], rows["new"])]
    print(f"paired CPU ratio new/old per pair: {', '.join(f'{x:.2f}' for x in ratios)}"
          f" -> median {statistics.median(ratios):.2f}")
    norm = [(n["cpu"] / n["decode_ms"]) / (o["cpu"] / o["decode_ms"])
            for o, n in zip(rows["old"], rows["new"])]
    print(f"speed-normalised ratio (CPU / machine decode ms): median {statistics.median(norm):.2f}")


if __name__ == "__main__":
    main()
