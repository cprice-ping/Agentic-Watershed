"""
LoRA fine-tune experiment — distills a domain agent's real Haiku history
into a small local model, using the training_examples.jsonl produced by
extract_training_data.py.

DOES NOT RUN ON THE PI. This needs a GPU — Colab, RunPod, Vast.ai, or a
local GPU box. Copy this file (and the domain's training_examples.jsonl)
to wherever that is.

Setup on the training machine:
  pip install "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git"
  pip install trl datasets

  Unsloth's exact API moves fast — if something below doesn't match
  current unsloth/trl versions, check https://github.com/unslothai/unsloth
  for the current FastLanguageModel / SFTTrainer usage and adjust; the
  overall shape (load 4bit -> add LoRA -> SFTTrainer -> save_pretrained_gguf)
  should still hold.

Usage:
  python3 finetune.py --domain weather --data training_examples.jsonl
  python3 finetune.py --domain weather --data training_examples.jsonl --base-model unsloth/Qwen2.5-3B-Instruct-bnb-4bit

Output:
  <domain>-lora-adapter/      LoRA adapter weights
  <domain>-merged-gguf/       Merged model, GGUF format — `ollama create` this
  <domain>-holdout.jsonl      The held-out 20%, NOT trained on — copy back to
                               the Pi and run through ollama_pilot_test.py-style
                               comparison against the base (untrained) model.
"""

import argparse
import json
import random
from pathlib import Path

# Same system prompts as the real agents / pilot scripts — kept as literal
# constants here since this script may run in a completely separate
# environment (GPU rental box) that doesn't have the rest of the repo.

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
    """One example -> a chat-formatted training row: system + user (context)
    + assistant (the real historical response, as the target completion)."""
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
    parser = argparse.ArgumentParser(description="LoRA fine-tune a domain agent's local model")
    parser.add_argument("--domain", required=True, choices=list(SYSTEM_PROMPTS.keys()))
    parser.add_argument("--data", required=True, type=Path, help="Path to training_examples.jsonl")
    parser.add_argument("--base-model", default="unsloth/Qwen2.5-3B-Instruct-bnb-4bit",
                        help="HF model id (4bit-quantized Unsloth variant recommended)")
    parser.add_argument("--holdout-frac", type=float, default=0.2)
    parser.add_argument("--lora-r", type=int, default=8, help="Low rank — small dataset, don't overparameterize")
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--epochs", type=int, default=3,
                        help="Small dataset (~100 examples) — watch held-out eval, not just loss, "
                             "for overfitting before increasing this")
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)

    examples = load_examples(args.data)
    print(f"Loaded {len(examples)} examples for domain={args.domain}")

    train_examples, holdout_examples = temporal_split(examples, args.holdout_frac)
    print(f"Train: {len(train_examples)}  |  Holdout (most recent, untouched): {len(holdout_examples)}")

    holdout_path = args.output_dir / f"{args.domain}-holdout.jsonl"
    with open(holdout_path, "w") as f:
        for ex in holdout_examples:
            f.write(json.dumps(ex) + "\n")
    print(f"Held-out set written to {holdout_path} — copy this back to the Pi for eval, "
          f"do not train on it.")

    train_chat = [to_chat_example(ex, args.domain) for ex in train_examples]

    # --- Unsloth / trl training ---
    # Imported here, not at module level, so --data/--domain validation and
    # the holdout split above still work without a GPU environment present
    # (useful for a quick dry-run of the data pipeline before committing to
    # a GPU rental session).
    from datasets import Dataset
    from unsloth import FastLanguageModel
    from trl import SFTTrainer, SFTConfig

    print(f"\nLoading base model: {args.base_model}")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.base_model,
        max_seq_length=4096,  # contexts run a few KB — see extract_training_data.py output
        load_in_4bit=True,
    )

    model = FastLanguageModel.get_peft_model(
        model,
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        lora_dropout=0.05,
        bias="none",
        random_state=args.seed,
    )

    def format_row(row):
        return {"text": tokenizer.apply_chat_template(row["messages"], tokenize=False, add_generation_prompt=False)}

    dataset = Dataset.from_list(train_chat).map(format_row)

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="text",
        max_seq_length=4096,
        args=SFTConfig(
            per_device_train_batch_size=2,
            gradient_accumulation_steps=4,
            num_train_epochs=args.epochs,
            learning_rate=2e-4,
            warmup_ratio=0.1,
            logging_steps=5,
            output_dir=str(args.output_dir / f"{args.domain}-training-run"),
            seed=args.seed,
        ),
    )

    print("\nTraining...")
    trainer.train()

    adapter_dir = args.output_dir / f"{args.domain}-lora-adapter"
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    print(f"LoRA adapter saved to {adapter_dir}")

    gguf_dir = args.output_dir / f"{args.domain}-merged-gguf"
    print(f"\nExporting merged GGUF to {gguf_dir} (q4_k_m, matches the quantization already "
          f"benchmarked in the base-model pilot tests)...")
    model.save_pretrained_gguf(str(gguf_dir), tokenizer, quantization_method="q4_k_m")

    print(f"""
Done. Next steps:
  1. Copy {gguf_dir}/*.gguf and {holdout_path} back to the Pi.
  2. ollama create {args.domain}-finetuned -f Modelfile   (point the Modelfile FROM at the .gguf)
  3. Run the held-out examples through {args.domain}/ollama_pilot_test.py-style comparison
     against both the base model and {args.domain}-finetuned — same real inputs, diff the outputs.
""")


if __name__ == "__main__":
    main()
