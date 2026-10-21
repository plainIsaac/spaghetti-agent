"""Run a queue of benchmark jobs, resumable across provider usage windows.

    python -m benchmarks.matrix benchmarks/matrices/model-strength.json

Each job is `{"benchmark": "context_economics", "arm": ..., "model": ..., "label": ...}`.
Completed jobs (a results directory ending in `-<arm>-<label>` with `run.json`)
are skipped, so rerunning the same matrix continues where it stopped. When a
provider usage window is exhausted the partial job is discarded and this exits
with status 75 after printing `QUOTA ... (resets in Ns)`.
"""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

HERE = Path(__file__).resolve().parent
QUOTA_EXIT = 75


def completed(job: dict) -> bool:
    results = HERE / job["benchmark"] / "results"
    suffix = f"-{job['arm']}-{job['label']}"
    return any(path.name.endswith(suffix) and (path / "run.json").exists() for path in results.glob(f"*{suffix}"))


def main() -> None:
    jobs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    remaining = [job for job in jobs if not completed(job)]
    print(f"{len(jobs) - len(remaining)}/{len(jobs)} jobs already complete", flush=True)
    for job in remaining:
        print(f"→ {job['arm']} {job['model']} {job['label']}", flush=True)
        process = subprocess.run(
            [sys.executable, "-m", f"benchmarks.{job['benchmark']}.run", "--arm", job["arm"], "--model", job["model"], "--label", job["label"]],
            cwd=HERE.parent, capture_output=True, text=True,
        )
        tail = (process.stdout.strip().splitlines() or [""])[-1]
        print(f"  {tail}", flush=True)
        if process.returncode == QUOTA_EXIT:
            raise SystemExit(QUOTA_EXIT)
        if process.returncode != 0:
            print(f"  failed with status {process.returncode}: {process.stderr.strip()[-300:]}", flush=True)
    print("matrix complete", flush=True)


if __name__ == "__main__":
    main()
