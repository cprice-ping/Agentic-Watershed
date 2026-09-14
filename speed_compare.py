"""
Speed and token comparison — the real prompts, two backends.

Throwaway experiment, NOT wired into any agent. Answers one question: how
fast is a given model on the exact work the domain agents do twice a day,
and what does it cost in tokens to do it.

Motivated by Inception's mercury-2.5, a diffusion LLM that refines tokens in
parallel rather than emitting them one at a time and claims ~1,100 output
tokens/sec. Whether that holds on a 35,000-token watershed prompt with a
forced structured answer is not something a benchmark page can say.

WHAT MAKES THIS A FAIR TEST
  - The system prompt comes from the live agent module, imported, not copied.
    The earlier pilot scripts kept their own copies and needed a commit
    ("Propagate the flag-criteria clarifications to the pilot scripts") every
    time the real prompt moved. A copy that drifts measures the wrong prompt.
  - The context is a real historical one, replayed by extract_training_data.py
    with an as-of cutoff, so it is the exact text that ran that day.
  - Calls alternate order per example, so neither backend systematically gets
    the warm connection.
  - The first call to each backend is discarded as warm-up.

WHAT MAKES IT UNFAIR, AND CANNOT BE FIXED HERE
  - Different transport. Anthropic is called directly; OpenRouter is a broker
    that adds a hop and picks an upstream provider per request. Some of any
    gap is routing, not the model. Run --backend-base against the provider
    directly to separate them.
  - Different structuring mechanism. The agents force a tool call; OpenRouter
    gets the same schema as a json_schema response format. Equivalent in
    intent, not in machinery.
  - Input token counts are NOT comparable between vendors. Different
    tokenisers over identical text give different numbers, so treat input
    tokens as a per-model cost input, never as a measure of "the same prompt".
    Output tokens are comparable as effort, since both are answering the same
    question in the same schema.

Usage:
  python3 extract_training_data.py --domain fire        # once, if not done
  export OPENROUTER_API_KEY=...        # and ANTHROPIC_API_KEY for the baseline
  python3 speed_compare.py --domain fire --n 6
  python3 speed_compare.py --domain fire --n 6 --model inception/mercury-2
  python3 speed_compare.py --domain fire --n 6 --reasoning none
  python3 speed_compare.py --domain river --n 4 --only openrouter
"""

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).parent

AGENTS = {
    "fire":    BASE / "Fire" / "agent.py",
    "weather": BASE / "Weather" / "agent.py",
    "aqi":     BASE / "AQI" / "agent.py",
    "river":   BASE / "River" / "agent.py",
}
DATA = {
    "fire":    BASE / "Fire" / "data" / "training_examples.jsonl",
    "weather": BASE / "Weather" / "data" / "training_examples.jsonl",
    "aqi":     BASE / "AQI" / "data" / "training_examples.jsonl",
    "river":   BASE / "River" / "data" / "training_examples.jsonl",
}

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Per million tokens. From the model pages on 2026-09-14 — re-check before
# quoting these anywhere, vendor pricing moves and this file will not.
PRICING = {
    "claude-haiku-4-5":      (1.00, 5.00),
    "inception/mercury-2.5": (0.04, 0.15),
    "inception/mercury-2":   (0.04, 0.15),
}
DEFAULT_OPENROUTER_MODEL = "inception/mercury-2.5"
BASELINE_MODEL = "claude-haiku-4-5"


def load_agent(domain: str):
    """Import the live agent module for its real SYSTEM_PROMPT and tool schema.

    Imported rather than duplicated: this is the prompt that actually runs,
    including whatever was changed this week.
    """
    path = AGENTS[domain]
    spec = importlib.util.spec_from_file_location(f"{domain}_agent", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def load_examples(path: Path, n: int) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    rows.sort(key=lambda e: e["observed_at"])
    if n >= len(rows):
        return rows
    # Evenly spread rather than the most recent N: context length varies with
    # how much was happening, and a week of quiet skews the timing.
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def call_anthropic(model: str, system: str, tool: dict, context: str,
                   max_tokens: int = 2048) -> dict:
    """The agents' exact request shape: forced tool use, same max_tokens."""
    import anthropic
    client = anthropic.Anthropic()
    started = time.perf_counter()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": context}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
    )
    elapsed = time.perf_counter() - started
    block = next((b for b in msg.content if b.type == "tool_use"), None)
    return {
        "seconds": elapsed,
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "parsed": block.input if block else None,
        "stop": msg.stop_reason,
    }


def call_openrouter(model: str, system: str, tool: dict, context: str,
                    reasoning: str | None, max_tokens: int = 2048) -> dict:
    """Same schema, expressed the way an OpenAI-shaped API takes it."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set — https://openrouter.ai/keys")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": context},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": tool["name"], "strict": True,
                            "schema": tool["input_schema"]},
        },
        "max_tokens": max_tokens,
    }
    # Omitted by default so the first run measures the model's out-of-box
    # behaviour. mercury-2.5 has tunable reasoning; --reasoning is how you
    # find out what it costs in latency.
    if reasoning:
        payload["reasoning"] = {"effort": reasoning}

    started = time.perf_counter()
    resp = httpx.post(OPENROUTER_URL, json=payload, timeout=180,
                      headers={"Authorization": f"Bearer {key}"})
    resp.raise_for_status()
    body = resp.json()
    elapsed = time.perf_counter() - started

    choice = (body.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content")
    usage = body.get("usage") or {}
    try:
        parsed = json.loads(text) if text else None
    except (json.JSONDecodeError, TypeError):
        parsed = None
    # Reasoning tokens are billed as completion tokens but never appear in
    # content, so a model that reasons by default looks like a model that
    # writes enormously verbose answers. Separating them is what turns "it
    # emitted 1,990 tokens" into "it spent 1,900 of them thinking".
    detail = usage.get("completion_tokens_details") or {}
    return {
        "seconds": elapsed,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": detail.get("reasoning_tokens"),
        "parsed": parsed,
        "stop": choice.get("finish_reason"),
        # Which upstream OpenRouter actually routed to. Worth printing: the
        # same slug can land on different providers run to run, and that
        # alone moves latency.
        "provider": body.get("provider"),
    }


def cost_usd(model: str, tok_in, tok_out) -> float | None:
    price = PRICING.get(model)
    if not price or tok_in is None or tok_out is None:
        return None
    return (tok_in / 1e6) * price[0] + (tok_out / 1e6) * price[1]


def is_truncated(run: dict) -> bool:
    """Did this call get cut off rather than finish?

    A truncated call has not done the task, so its latency is the time to
    emit max_tokens and stop — not the time to answer. Averaging those into
    a speed figure produces a number that looks like a measurement and is
    not one.
    """
    return run.get("stop") in ("length", "max_tokens") or run.get("parsed") is None


def summarise(label: str, model: str, runs: list[dict]) -> None:
    if not runs:
        print(f"\n{label} ({model}): no successful runs")
        return

    truncated = [r for r in runs if is_truncated(r)]
    runs = [r for r in runs if not is_truncated(r)]
    if truncated:
        print(f"\n{label}  ({model})")
        print(f"  *** {len(truncated)} of {len(truncated) + len(runs)} call(s) "
              f"hit the token cap or returned nothing parseable.")
        print("  *** Those did not answer, so their latency is the time to be"
              "\n  *** cut off. They are excluded below; with any of them "
              "present the\n  *** comparison is not yet valid. Try --reasoning "
              "none, or raise\n  *** --max-tokens, and re-run until this line "
              "is gone.")
    if not runs:
        print("  No completed calls to summarise.")
        return
    secs = [r["seconds"] for r in runs]
    outs = [r["output_tokens"] for r in runs if r["output_tokens"]]
    ins = [r["input_tokens"] for r in runs if r["input_tokens"]]
    costs = [c for c in (cost_usd(model, r["input_tokens"], r["output_tokens"])
                         for r in runs) if c is not None]

    def p90(xs):
        return sorted(xs)[min(len(xs) - 1, int(len(xs) * 0.9))]

    if not truncated:
        print(f"\n{label}  ({model}, {len(runs)} run(s))")
    else:
        print(f"  --- over the {len(runs)} completed call(s) ---")
    print(f"  latency      median {statistics.median(secs):6.2f}s   "
          f"mean {statistics.fmean(secs):6.2f}s   "
          f"p90 {p90(secs):6.2f}s   "
          f"min {min(secs):.2f}s   max {max(secs):.2f}s")
    if ins:
        print(f"  input tok    median {statistics.median(ins):8.0f}   "
              f"total {sum(ins):8.0f}")
    if outs:
        print(f"  output tok   median {statistics.median(outs):8.0f}   "
              f"total {sum(outs):8.0f}")
        # Billed as completion tokens, absent from content. Without this line
        # a model that reasons by default reads as one that writes essays.
        reas = [r["reasoning_tokens"] for r in runs if r.get("reasoning_tokens")]
        if reas:
            print(f"    of which reasoning  median {statistics.median(reas):6.0f}"
                  f"   total {sum(reas):8.0f}"
                  f"   ({100 * sum(reas) / sum(outs):.0f}% of output)")
        rate = [r["output_tokens"] / r["seconds"] for r in runs
                if r["output_tokens"] and r["seconds"]]
        if rate:
            print(f"  output tok/s median {statistics.median(rate):6.1f}   "
                  f"mean {statistics.fmean(rate):6.1f}")
    if costs:
        print(f"  cost         ${sum(costs):.5f} over {len(costs)} run(s)  "
              f"(${statistics.fmean(costs):.5f}/run, "
              f"${statistics.fmean(costs) * 2 * 365:.2f}/yr at 2 runs/day)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--domain", required=True, choices=sorted(AGENTS))
    ap.add_argument("--n", type=int, default=6, help="historical contexts to replay")
    ap.add_argument("--model", default=DEFAULT_OPENROUTER_MODEL,
                    help=f"OpenRouter model (default {DEFAULT_OPENROUTER_MODEL})")
    ap.add_argument("--baseline", default=BASELINE_MODEL,
                    help=f"Anthropic model to compare against (default {BASELINE_MODEL})")
    ap.add_argument("--reasoning", default=None,
                    choices=["none", "low", "medium", "high"],
                    help="OpenRouter reasoning effort; omitted entirely if unset")
    ap.add_argument("--max-tokens", type=int, default=2048,
                    help="output cap for both sides (default 2048, what the "
                         "agents use). Raise it to tell 'the model is verbose' "
                         "apart from 'the reasoning trace ate the budget'.")
    ap.add_argument("--only", choices=["anthropic", "openrouter"], default=None,
                    help="run just one side")
    ap.add_argument("--data", type=Path, default=None)
    args = ap.parse_args()

    data_path = args.data or DATA[args.domain]
    if not data_path.exists():
        print(f"No replayed contexts at {data_path}\n"
              f"Run: python3 extract_training_data.py --domain {args.domain}")
        sys.exit(1)

    agent = load_agent(args.domain)
    system = agent.SYSTEM_PROMPT
    tool = agent._ASSESSMENT_TOOL
    examples = load_examples(data_path, args.n + 1)   # +1 for the warm-up

    print(f"domain {args.domain}  |  {len(examples) - 1} scored context(s) "
          f"+ 1 warm-up, spread {examples[0]['observed_at'][:10]} to "
          f"{examples[-1]['observed_at'][:10]}")
    print(f"system prompt: {len(system):,} chars, imported live from "
          f"{AGENTS[args.domain].relative_to(BASE)}")
    if args.reasoning:
        print(f"openrouter reasoning effort: {args.reasoning}")
    print()

    anth_runs, or_runs = [], []
    providers = set()

    for i, ex in enumerate(examples):
        warm = i == 0
        tag = "warm-up" if warm else f"{i}/{len(examples) - 1}"
        ctx = ex["context"]
        # Alternate which backend goes first so neither is consistently the
        # one paying for a cold connection.
        order = ["anthropic", "openrouter"] if i % 2 == 0 else ["openrouter", "anthropic"]
        if args.only:
            order = [args.only]

        line = [f"[{tag}] {ex['observed_at'][:16]}  ctx {len(ctx):,} chars"]
        for backend in order:
            try:
                if backend == "anthropic":
                    r = call_anthropic(args.baseline, system, tool, ctx,
                                       args.max_tokens)
                    if not warm:
                        anth_runs.append(r)
                    name = "haiku"
                else:
                    r = call_openrouter(args.model, system, tool, ctx,
                                        args.reasoning, args.max_tokens)
                    if r.get("provider"):
                        providers.add(r["provider"])
                    if not warm:
                        or_runs.append(r)
                    name = "openrouter"
                line.append(f"{name} {r['seconds']:5.2f}s "
                            f"{r['output_tokens'] or '?'}out")
                if r["parsed"] is None:
                    line.append(f"[{name}: UNPARSEABLE stop={r['stop']}]")
            except Exception as exc:                        # noqa: BLE001
                line.append(f"{backend} FAILED: {str(exc)[:120]}")
        print("  ".join(line))

    print("\n" + "=" * 68)
    summarise("ANTHROPIC ", args.baseline, anth_runs)
    summarise("OPENROUTER", args.model, or_runs)
    if providers:
        print(f"\n  openrouter routed to: {', '.join(sorted(providers))}")

    if anth_runs and or_runs:
        a = statistics.median([r["seconds"] for r in anth_runs])
        o = statistics.median([r["seconds"] for r in or_runs])
        faster, factor = ("openrouter", a / o) if o < a else ("anthropic", o / a)
        print(f"\n  {faster} is {factor:.1f}x faster on median wall clock.")
        print("  Some of that is the OpenRouter hop rather than the model — "
              "\n  a direct provider call is the way to separate them.")

    print("\n  Input token counts are not comparable across vendors: different"
          "\n  tokenisers over identical text. Output tokens are comparable as"
          "\n  effort, and are what drives both latency and output cost.")


if __name__ == "__main__":
    main()
