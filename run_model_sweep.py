"""
Throwaway sweep runner — NOT wired into any agent.

Runs the same compare_backend_to_haiku.py comparison across a list of
candidate (backend, model) pairs, unmodified — same domain, same
--flagged filter, same --after (or lack of one), so results are directly
comparable to each other and to runs already done by hand. Doesn't change
what's being tested, just automates repeating it per candidate.

Usage:
  python3 run_model_sweep.py --domain weather --flagged false
  python3 run_model_sweep.py --domain weather --flagged false --after 2026-07-25
  python3 run_model_sweep.py --domain fire --n 8

Edit CANDIDATES below to add/remove models.
"""

import argparse
import subprocess
import sys
from pathlib import Path

# (backend, model) — kept deliberately small and reasoning-oriented, since
# the question being asked is "does this model infer the unwritten intent
# of an ambiguous rule," not "can it follow an explicit one." Some of these
# are too large for local LM Studio at reasonable speed and go via Together
# instead — the together backend already has 429 retry/backoff wired in.
CANDIDATES = [
    ("lmstudio", "google/gemma-4-31b-qat"),   # already-tested baseline, for a like-for-like anchor
    ("together", "Qwen/QwQ-32B"),
    ("together", "Qwen/Qwen3-Next-80B-A3B-Thinking"),
    ("together", "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B"),
    ("together", "meta-llama/Llama-4-Scout-17B-16E-Instruct"),
]

BASE = Path(__file__).parent
COMPARE_SCRIPT = BASE / "compare_backend_to_haiku.py"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True, choices=["fire", "weather"])
    parser.add_argument("--n", type=int, default=8)
    parser.add_argument("--after", default=None)
    parser.add_argument("--flagged", choices=["true", "false"], default=None)
    args = parser.parse_args()

    results_dir = BASE / "sweep_results"
    results_dir.mkdir(exist_ok=True)

    for backend, model in CANDIDATES:
        safe_model = model.replace("/", "_")
        log_path = results_dir / f"{args.domain}_{backend}_{safe_model}.log"

        cmd = [
            sys.executable, str(COMPARE_SCRIPT),
            "--domain", args.domain,
            "--n", str(args.n),
            "--backend", backend,
            "--model", model,
        ]
        if args.after:
            cmd += ["--after", args.after]
        if args.flagged:
            cmd += ["--flagged", args.flagged]

        print(f"\n{'=' * 70}")
        print(f"Running: {backend} / {model}")
        print(f"Logging to: {log_path}")
        print(f"{'=' * 70}")

        with open(log_path, "w") as log_file:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            log_file.write(proc.stdout)
            print(proc.stdout)

        if proc.returncode != 0:
            print(f"!!! {model} exited with code {proc.returncode} — see {log_path}")

    print(f"\n{'=' * 70}")
    print(f"All done. Full logs in {results_dir}/")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
