"""
LoRA fine-tune experiment — distills a domain agent's real Haiku history
into a small local model, using the training_examples.jsonl produced by
extract_training_data.py.

Runs on Apple Silicon (MLX), not the Pi. Tested-shape for a 64GB M-series
Mac — a 3-4B model in bf16 (no quantization needed at that memory size)
is ~6-8GB, comfortable headroom for LoRA on top.

Setup on the Mac:
  pip install mlx-lm

  mlx-lm's exact CLI flags move with the library — this script prepares
  the data and prints the recommended `mlx_lm.lora` command by default;
  pass --run to have it attempt the subprocess call directly. If --run's
  flags don't match your installed version, check `mlx_lm.lora --help`
  and adjust — the printed command is the reference either way.

Usage:
  python3 finetune.py --domain weather --data training_examples.jsonl
  python3 finetune.py --domain weather --data training_examples.jsonl --run

Output:
  <domain>-mlx-data/            train.jsonl / valid.jsonl in MLX's chat format
  <domain>-lora-adapter/        LoRA adapter weights (after training)
  <domain>-holdout.jsonl        Held-out 20%, NOT trained on — for the
                                 before/after comparison against the base model

Evaluating the result (directly on the Mac, no Ollama/GGUF round-trip needed
for this experimental phase):
  mlx_lm.generate --model <base-model> --adapter-path <domain>-lora-adapter \
      --prompt "<context from a held-out example>"
  # compare against the same prompt without --adapter-path (base model)
"""

import argparse
import json
import subprocess
from pathlib import Path

# Same system prompts as the real agents / pilot scripts — kept as literal
# constants here since this script may run in a completely separate
# environment (a different machine) that doesn't have the rest of the repo.

SYSTEM_PROMPTS = {
    "weather": """You are an autonomous weather monitoring agent for Napa County, California.
You run on a schedule with no human present. Your focus is on conditions relevant to:
  - Fire weather risk (temperature, humidity, wind, recent precipitation)
  - Flood/precipitation risk (rainfall amounts, trends)
  - Any active NWS watches or warnings

Fire weather flag criteria (flag if ANY are true):
- Active Red Flag Warning or Fire Weather Watch
- Temperature >= 90F AND humidity <= 25% AND wind >= 15 mph
- Humidity <= 15% regardless of other factors
- Wind gusts >= 45 mph

Flood flag criteria:
- Active Flood Watch or Warning
- Precipitation > 25mm in 1 hour
- Precipitation > 50mm in 24 hours

Be specific about values. Reference actual F, %, mph readings.
Note wind direction — offshore (NE/E) winds in Napa are Diablo winds and especially dangerous for fire.
""",
    "fire": """You are an autonomous fire-detection monitoring agent for Napa Valley, California.
Your only job: is there an actual satellite-detected heat source (a "hotspot") near Napa Valley
right now, based on NASA FIRMS thermal detections. Not fire weather, not smoke — a real detected
heat source.

Flag (flagged=true) if ANY of these are true:
- Any hotspot detected within 20 miles of Napa Valley center
- Any high-confidence hotspot within 50 miles
- FRP (fire radiative power) rising across consecutive polls for a hotspot in range
- A new hotspot cluster appeared since the last observation that wasn't there before
- The collector's last poll status is "error"
""",
    "aqi": """You are an autonomous air quality monitoring agent for Napa County, California.
PM2.5 is the primary wildfire smoke indicator. A sudden AQI rise, especially when weather
conditions don't explain it, often means a fire has started upwind.""",
    "river": """You are an autonomous watershed monitoring agent for the Napa River.
Assess streamflow and gage height against flood risk and drought/baseline conditions.""",
}

RESPONSE_SCHEMA_HINT = (
    'Respond with exactly this JSON shape: '
    '{"summary": "...", "flagged": true|false, "reasoning": "..."}'
)


def load_examples(data_path: Path) -> list[dict]:
    examples = []
    with open(data_path) as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    return examples


def temporal_split(examples: list[dict], holdout_frac: float = 0.2) -> tuple[list[dict], list[dict]]:
    """Train on the earlier examples, hold out the most recent ones — tests
    forward generalization, not just memorization of a random subset."""
    examples_sorted = sorted(examples, key=lambda e: e["observed_at"])
    n_holdout = max(1, int(len(examples_sorted) * holdout_frac))
    train = examples_sorted[:-n_holdout]
    holdout = examples_sorted[-n_holdout:]
    return train, holdout


def to_chat_example(example: dict, domain: str) -> dict:
    """One example -> MLX-LM's chat training format: {"messages": [...]}.
    mlx_lm.lora applies the model's chat template to this automatically."""
    system_prompt = SYSTEM_PROMPTS[domain] + "\n\n" + RESPONSE_SCHEMA_HINT
    assistant_content = json.dumps(example["response"])
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": example["context"]},
            {"role": "assistant", "content": assistant_content},
        ]
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="LoRA fine-tune a domain agent's local model (MLX / Apple Silicon)")
    parser.add_argument("--domain", required=True, choices=list(SYSTEM_PROMPTS.keys()))
    parser.add_argument("--data", required=True, type=Path, help="Path to training_examples.jsonl")
    parser.add_argument("--base-model", default="mlx-community/Qwen2.5-3B-Instruct-bf16",
                        help="MLX-format HF repo id. Check https://huggingface.co/mlx-community "
                             "for the current exact name — if it's not there, `mlx_lm.convert "
                             "--hf-path Qwen/Qwen2.5-3B-Instruct` converts the original repo locally.")
    parser.add_argument("--holdout-frac", type=float, default=0.2)
    parser.add_argument("--lora-layers", type=int, default=8,
                        help="Number of layers to apply LoRA to — mlx_lm default is fine for a "
                             "small dataset like this; raise only if you have more examples")
    parser.add_argument("--iters", type=int, default=200,
                        help="Small dataset (~100 examples) — watch validation loss / held-out "
                             "eval for overfitting before raising this")
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--run", action="store_true",
                         help="Actually shell out to mlx_lm.lora. Without this flag, only "
                              "prepares data and prints the command.")
    args = parser.parse_args()

    examples = load_examples(args.data)
    print(f"Loaded {len(examples)} examples for domain={args.domain}")

    train_examples, holdout_examples = temporal_split(examples, args.holdout_frac)
    print(f"Train: {len(train_examples)}  |  Holdout (most recent, untouched): {len(holdout_examples)}")

    holdout_path = args.output_dir / f"{args.domain}-holdout.jsonl"
    with open(holdout_path, "w") as f:
        for ex in holdout_examples:
            f.write(json.dumps(ex) + "\n")
    print(f"Held-out set written to {holdout_path} — for before/after eval, not for training.")

    # mlx_lm.lora expects a directory containing train.jsonl and valid.jsonl
    # in its chat format. We don't have a separate validation split beyond
    # the holdout — carve a small slice off the *training* set for that
    # (still never touching holdout), since mlx_lm.lora wants one to report
    # validation loss during training.
    n_valid = max(1, int(len(train_examples) * 0.1))
    valid_examples = train_examples[-n_valid:]
    fit_examples = train_examples[:-n_valid]

    data_dir = args.output_dir / f"{args.domain}-mlx-data"
    data_dir.mkdir(exist_ok=True)
    with open(data_dir / "train.jsonl", "w") as f:
        for ex in fit_examples:
            f.write(json.dumps(to_chat_example(ex, args.domain)) + "\n")
    with open(data_dir / "valid.jsonl", "w") as f:
        for ex in valid_examples:
            f.write(json.dumps(to_chat_example(ex, args.domain)) + "\n")
    print(f"MLX training data written to {data_dir} "
          f"(fit={len(fit_examples)}, valid={len(valid_examples)})")

    adapter_dir = args.output_dir / f"{args.domain}-lora-adapter"
    cmd = [
        "mlx_lm.lora",
        "--model", args.base_model,
        "--train",
        "--data", str(data_dir),
        "--adapter-path", str(adapter_dir),
        "--num-layers", str(args.lora_layers),
        "--iters", str(args.iters),
        "--batch-size", "1",  # small dataset, small contexts vary in length — keep this simple
    ]
    print("\nCommand:\n  " + " ".join(cmd))

    if args.run:
        print("\nRunning...")
        subprocess.run(cmd, check=True)
        print(f"""
Done. Adapter saved to {adapter_dir}. Next steps:
  1. Compare base vs fine-tuned on the held-out set:
       mlx_lm.generate --model {args.base_model} --prompt "<context from {holdout_path}>"
       mlx_lm.generate --model {args.base_model} --adapter-path {adapter_dir} --prompt "<same context>"
  2. If it looks good and you want it on the Pi: fuse the adapter
     (mlx_lm.fuse) and convert to GGUF via llama.cpp's convert script —
     a manual, later step, only worth doing once the eval above looks right.
""")
    else:
        print("\n(--run not passed — data prepared, training not started. "
              "Re-run with --run to actually train, or copy the command above.)")


if __name__ == "__main__":
    main()
