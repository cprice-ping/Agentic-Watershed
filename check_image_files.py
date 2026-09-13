"""
Do the images contain the files their code reads?

Every Dockerfile here copies a hand-written list of paths, and every time a
module gains a dependency on a file outside that list the image breaks while
the Pi and the dev checkout stay fine — cron and a local run resolve every
path because the whole repo is there. The deployed thing is the only place
that fails, and it fails at import, before logging is configured.

Four instances so far, recorded in the Dockerfiles themselves:

  publishers.json     missing from the Synthesis image, twice. The subscriber
                      fell back to a built-in single DID and was correct by
                      coincidence; a second node would have been ignored with
                      nothing saying so.
  node_config.json    missing from the ATProto image, with each domain's
  + thresholds.py     thresholds.py — that image crashed on import.
  agent_runtime.py    missing from the root image (2026-09-10) and nearly
                      missing from the ATProto image (2026-09-12).

Three of those four are data files, not imports, so checking imports alone
would have caught one of them.

What this does NOT do: build anything. It reads COPY lines, works out which
files would land in the image, and asks whether the Python that lands there
can find what it reaches for. Static and fast enough to run on every change.
A real build would be better and is not available on the Pi.

Usage:
  python3 check_image_files.py          # exit 1 if anything is missing
  python3 check_image_files.py -v       # also list what each image contains
"""

import argparse
import ast
import re
import sys
from pathlib import Path

BASE = Path(__file__).parent

# Filenames a module might read at runtime. Deliberately narrow: these are
# the kinds this repo actually loads by path.
DATA_SUFFIXES = (".json", ".txt", ".sh", ".env")

# Filenames appearing as literals in the source. `.py` is in here because a
# module loaded by computed path is invisible to the import check:
# publisher.py reaches each domain's thresholds.py through
# `BASE / domain / "thresholds.py"` with importlib, and dropping one of those
# COPY lines was a real break that an AST import walk cannot see.
_DATA_LITERAL = re.compile(
    r"[\"']([A-Za-z0-9_.\-]+\.(?:json|txt|sh|env|py))[\"']")

# `- ./node_config.json:/app/node_config.json:ro`
_MOUNT = re.compile(r"^\s*-\s+(\.[^:\s]+):([^:\s]+)")

# `python /app/agent/agent_atproto.py \` in an entrypoint. A script the image
# runs is as much a dependency as one it imports, and nothing in Python
# references it — dropping `COPY agent/` from the Synthesis image is invisible
# to an AST walk and to a literal scan of .py files.
_SH_INVOKE = re.compile(r"(?:python3?|sh|bash)\s+(\S+\.(?:py|sh))")
# The WORKDIR everything here uses. Entrypoints name absolute in-image paths.
_IMAGE_ROOT = "/app/"


def mounted_at_runtime() -> set[str]:
    """Basenames supplied by a bind mount rather than baked into an image.

    Read from docker-compose.yml rather than kept as an ignore list here,
    because that file is what actually decides it. node_config.json is
    deliberately absent from the root image so one image serves any node —
    swap the mounted config, not the image — and a check that called that a
    defect would be wrong every time and quickly ignored.

    Basenames, not paths: this is a mount declared somewhere in the
    deployment, which is as much as a static read can honestly claim. It
    means the check will stay quiet about a file that is mounted for one
    service and genuinely missing from another image. Narrowing that needs
    the service-to-Dockerfile mapping, and the looser version has never been
    the failure — every instance so far was a file mounted nowhere.
    """
    out: set[str] = set()
    for compose in BASE.rglob("docker-compose*.y*ml"):
        for line in compose.read_text().splitlines():
            m = _MOUNT.match(line)
            if m:
                out.add(Path(m.group(1)).name)
    return out


def dockerfiles() -> list[Path]:
    return sorted(BASE.rglob("Dockerfile*"))


def copy_sources(dockerfile: Path) -> list[str]:
    """Source paths from COPY lines, ignoring --from stages and flags."""
    out = []
    for raw in dockerfile.read_text().splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = line.split()[1:]
        parts = [p for p in parts if not p.startswith("--")]
        if len(parts) < 2:
            continue
        out.extend(parts[:-1])          # last token is the destination
    return out


def build_context(dockerfile: Path, sources: list[str]) -> Path:
    """Which directory the COPY paths are relative to.

    Inferred rather than configured, so a new Dockerfile needs no entry here:
    whichever candidate resolves more of the COPY sources wins. The root and
    ATProto images build from the repo root; Synthesis builds from its own
    directory.
    """
    candidates = [dockerfile.parent, BASE]
    best, best_hits = dockerfile.parent, -1
    for cand in candidates:
        hits = sum(1 for s in sources if (cand / s).exists())
        if hits > best_hits:
            best, best_hits = cand, hits
    return best


def image_files(context: Path, sources: list[str]) -> set[Path]:
    """Every file that would exist in the image, relative to the context."""
    present: set[Path] = set()
    for src in sources:
        path = context / src
        if path.is_dir():
            for f in path.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts:
                    present.add(f.relative_to(context))
        elif path.is_file():
            present.add(path.relative_to(context))
    return present


def imported_names(source: str) -> set[str]:
    """Top-level module names imported by a Python source string."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports resolve inside the package, which is copied
            # wholesale or not at all — nothing for this check to say.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def resolve_module(name: str, importer: Path, context: Path):
    """Where *name* lives in the context, or None if it is not a repo module.

    Checked against the importer's own directory first, because the domain
    packages import siblings by bare name after putting their directory on
    sys.path. Anything that resolves nowhere is stdlib or a dependency, and
    not this script's business.
    """
    for parent in (importer.parent, Path(".")):
        for candidate in (parent / f"{name}.py", parent / name / "__init__.py"):
            if (context / candidate).is_file():
                return candidate
    return None


def check(dockerfile: Path, verbose: bool, mounted: set[str]) -> list[str]:
    sources = copy_sources(dockerfile)
    if not sources:
        return []
    context = build_context(dockerfile, sources)
    present = image_files(context, sources)
    rel = dockerfile.relative_to(BASE)

    if verbose:
        print(f"\n{rel}  (context: {context.relative_to(BASE) or '.'}, "
              f"{len(present)} file(s))")

    problems = []

    # Scripts the image invokes. Checked before the Python walk because a
    # missing entrypoint target is a startup failure, not an import one.
    for f in sorted(p for p in present if p.suffix == ".sh"):
        for target in sorted(set(_SH_INVOKE.findall((context / f).read_text()))):
            if not target.startswith(_IMAGE_ROOT):
                continue                      # relative or outside the image
            wanted = Path(target[len(_IMAGE_ROOT):])
            if wanted not in present:
                problems.append(
                    f"{rel}: {f} runs `{target}`, not copied")

    for f in sorted(present):
        if f.suffix != ".py":
            continue
        source = (context / f).read_text()

        for name in sorted(imported_names(source)):
            target = resolve_module(name, f, context)
            if target is None:
                continue                      # stdlib or third-party
            if target not in present:
                problems.append(
                    f"{rel}: {f} imports `{name}` -> {target}, not copied")

        # Data files read by name. A literal that names a file which exists
        # in the context but not in the image is the publishers.json shape.
        for literal in sorted(set(_DATA_LITERAL.findall(source))):
            if literal in mounted:
                continue
            matches = [p.relative_to(context) for p in context.rglob(literal)
                       if p.is_file() and "__pycache__" not in p.parts]
            if not matches:
                continue
            # Every match, not just "is at least one present". The name is
            # often assembled at runtime — `BASE / domain / "thresholds.py"`
            # resolves to a different file per domain — so one copied sibling
            # says nothing about the one the code will actually want. Missing
            # Fire/thresholds.py while Weather/thresholds.py was copied is
            # exactly the break that reached the ATProto image.
            for m in sorted(matches):
                if m not in present:
                    problems.append(
                        f"{rel}: {f} names `{literal}` ({m}), not copied")

    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    files = dockerfiles()
    if not files:
        print("No Dockerfiles found.")
        return

    mounted = mounted_at_runtime()
    all_problems = []
    for df in files:
        all_problems.extend(check(df, args.verbose, mounted))

    print(f"\nChecked {len(files)} Dockerfile(s)"
          + (f", {len(mounted)} runtime mount(s) excluded." if mounted else "."))
    if not all_problems:
        print("Every file the copied code reaches for is in its image.")
        return
    print(f"\n{len(all_problems)} missing file(s):\n")
    for p in all_problems:
        print(f"  {p}")
    print("\nEach of these breaks only the built image — a checkout resolves "
          "\nthem all, so nothing local will show it.")
    sys.exit(1)


if __name__ == "__main__":
    main()
