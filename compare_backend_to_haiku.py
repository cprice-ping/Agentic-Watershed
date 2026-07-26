"""
Throwaway comparison script — NOT wired into any agent.

Runs a spread of REAL historical (context, actual Haiku decision) pairs
from training_examples.jsonl (produced by extract_training_data.py)
through a pilot backend (Ollama local model or OpenRouter), and reports
agreement against Haiku's actual real decision for that exact context —
not "does this look plausible," a genuine ground-truth comparison.

Reuses Fire/ollama_pilot_test.py's and Weather/ollama_pilot_test.py's
SYSTEM_PROMPT/RESPONSE_SCHEMA/call_* functions directly rather than
duplicating them, so there's one source of truth for the request shape.

Usage:
  python3 extract_training_data.py --domain fire       # if not already done
  python3 compare_backend_to_haiku.py --domain fire --n 8
  python3 compare_backend_to_haiku.py --domain weather --n 8 --backend openrouter
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).parent

DOMAIN_MODULES = {
    "fire": BASE / "Fire" / "ollama_pilot_test.py",
    "weather": BASE / "Weather" / "ollama_pilot_test.py",
}
DOMAIN_DATA = {
    "fire": BASE / "Fire" / "data" / "training_examples.jsonl",
    "weather": BASE / "Weather" / "data" / "training_examples.jsonl",
}


def load_domain_module(domain: str):
    """Import Fire/ollama_pilot_test.py or Weather/ollama_pilot_test.py as a
    module, to reuse its exact SYSTEM_PROMPT/RESPONSE_SCHEMA/call_openrouter/
    call_ollama rather than re-deriving them and risking drift."""
    path = DOMAIN_MODULES[domain]
    spec = importlib.util.spec_from_file_location(f"{domain}_pilot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_examples(path: Path) -> list[dict]:
    examples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    return examples


def spread_sample(examples: list[dict], n: int) -> list[dict]:
    """Evenly spaced across the whole time range, not just the most recent
    N — a fair sample sees old and new conditions, not just whatever's
    happened lately."""
    examples_sorted = sorted(examples, key=lambda e: e["observed_at"])
    if n >= len(examples_sorted):
        return examples_sorted
    step = len(examples_sorted) / n
    return [examples_sorted[int(i * step)] for i in range(n)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True, choices=list(DOMAIN_MODULES.keys()))
    parser.add_argument("--data", type=Path, default=None,
                        help="Path to training_examples.jsonl (default: <Domain>/data/training_examples.jsonl)")
    parser.add_argument("--n", type=int, default=8, help="Number of historical examples to test")
    parser.add_argument("--backend", choices=["ollama", "openrouter"], default="openrouter")
    parser.add_argument("--model", default=None)
    parser.add_argument("--after", default=None,
                         help="ISO date/datetime — exclude examples with observed_at before this. "
                              "Use to skip history from before a since-fixed bug so its stale "
                              "ground truth doesn't get counted as a model disagreement. e.g. "
                              "--after 2026-07-15 to exclude pre-multi-source-fix Fire history.")
    args = parser.parse_args()

    data_path = args.data or DOMAIN_DATA[args.domain]
    if not data_path.exists():
        print(f"No training data at {data_path} — run extract_training_data.py --domain {args.domain} first.")
        sys.exit(1)

    mod = load_domain_module(args.domain)
    model = args.model or mod.DEFAULT_MODEL[args.backend]

    examples = load_examples(data_path)
    if args.after:
        before_count = len(examples)
        examples = [e for e in examples if e["observed_at"] >= args.after]
        excluded = before_count - len(examples)
        if excluded:
            print(f"Excluded {excluded} example(s) before {args.after}")
        if not examples:
            print(f"No examples remain after filtering to --after {args.after}")
            sys.exit(1)

    sample = spread_sample(examples, args.n)
    print(f"Testing {len(sample)} of {len(examples)} real historical examples "
          f"(evenly spread across {sample[0]['observed_at']} to {sample[-1]['observed_at']})\n")
    print(f"Backend: {args.backend}  |  Model: {model}\n")

    agree = disagree = failed = 0
    disagreements = []

    for i, example in enumerate(sample, 1):
        context = example["context"]
        real = example["response"]

        try:
            if args.backend == "ollama":
                result, elapsed = mod.call_ollama(model, context, think=False)
            else:
                result, elapsed = mod.call_openrouter(model, context)
        except Exception as exc:
            print(f"[{i}/{len(sample)}] {example['observed_at']}: REQUEST FAILED — {exc}")
            failed += 1
            continue

        mod.check_finish_reason(args.backend, result)
        response_text = mod.extract_response_text(args.backend, result)
        try:
            parsed = json.loads(response_text)
        except (json.JSONDecodeError, TypeError):
            print(f"[{i}/{len(sample)}] {example['observed_at']}: FAILED TO PARSE ({elapsed:.1f}s)")
            failed += 1
            continue

        model_flagged = bool(parsed.get("flagged"))
        real_flagged = bool(real["flagged"])
        marker = "AGREE" if model_flagged == real_flagged else "DISAGREE"
        if model_flagged == real_flagged:
            agree += 1
        else:
            disagree += 1
            disagreements.append((example, parsed))

        print(f"[{i}/{len(sample)}] {example['observed_at']}: {marker} "
              f"(real={real_flagged}, model={model_flagged})  [{elapsed:.1f}s]")

    print(f"\n=== Summary: {agree} agree, {disagree} disagree, {failed} failed to run/parse "
          f"(out of {len(sample)}) ===")

    if disagreements:
        print("\n=== Disagreement detail ===")
        for example, parsed in disagreements:
            print(f"\n--- {example['observed_at']} ---")
            print(f"REAL   (Haiku, flagged={example['response']['flagged']}): {example['response']['summary']}")
            print(f"MODEL  (flagged={parsed.get('flagged')}): {parsed.get('summary')}")
            print(f"MODEL reasoning: {parsed.get('reasoning')}")


if __name__ == "__main__":
    main()
