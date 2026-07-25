"""
OnlyPans Test Publisher
-----------------------
Writes a pan record into your own repo so the rest of the pipeline can be
exercised end to end before any OAuth or UI exists.

This uses an app password, which is deliberately the wrong thing to ship. A
real OnlyPans client has to do ATProto OAuth, because asking contributors to
paste an app password into someone's hobby site is not an acceptable thing to
ask. App passwords are fine here for exactly one reason: you are publishing to
your own account, from your own laptop, to see records land in the index.

Works against any PDS. Against bsky.social it writes a custom-lexicon record
into an ordinary Bluesky account — which is the whole bet: contributors keep
the identity they already have and OnlyPans just stores different records in
it. Nothing about the record will render on bsky.app; only the app view knows
what a pan is.

Usage:
  export BSKY_HANDLE=you.bsky.social
  export BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx

  python publish_pan.py --title "3.5mm Dehillerin rondeau" \\
      --photo ~/pans/rondeau-top.jpg --alt "Top view, freshly retinned" \\
      --maker "E. Dehillerin" --form rondeau --lining tin \\
      --diameter-mm 280 --thickness-mm 3.5 --era "late 19th c." \\
      --condition retinned --notes "Estate sale in Sonoma, 2019."

  python publish_pan.py --title "Test" --dry-run     # build the record, print it, stop
  python publish_pan.py --list                       # show what's already in your repo
  python publish_pan.py --delete <rkey>              # remove one
"""

import argparse
import json
import os
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

LEXICON = "net.cpricedomain.onlypans.pan"
DEFAULT_PDS = "https://bsky.social"

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Matches the lexicon's maxSize. Checked client-side so an oversized photo
# fails with a useful message instead of a lexicon validation error.
MAX_BLOB_BYTES = 2_000_000


# ---------------------------------------------------------------------------
# Image dimensions — header parsing only, no imaging dependency
# ---------------------------------------------------------------------------

def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read (width, height) from PNG, JPEG, or WebP headers.

    Only used to populate aspectRatio, which lets the app view reserve layout
    space before a photo loads. Returns None rather than raising — a missing
    aspect ratio costs a layout shift, not a failed publish.
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w, h

        if data[:2] == b"\xff\xd8":  # JPEG
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                # SOF0-SOF15, excluding the non-frame markers in that range
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg_len
            return None

        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt = data[12:16]
            if fmt == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if fmt == b"VP8L":
                b = struct.unpack("<I", data[21:25])[0]
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if fmt == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
    except (struct.error, IndexError):
        return None
    return None


# ---------------------------------------------------------------------------
# ATProto
# ---------------------------------------------------------------------------

def login(pds: str, handle: str, password: str) -> dict:
    resp = httpx.post(
        f"{pds}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": password},
        timeout=30,
    )
    if resp.status_code == 401:
        sys.exit("Login failed. For bsky.social use an app password, not your account password.")
    resp.raise_for_status()
    return resp.json()


def upload_blob(pds: str, token: str, path: Path) -> dict:
    data = path.read_bytes()

    if len(data) > MAX_BLOB_BYTES:
        sys.exit(
            f"{path.name} is {len(data) / 1e6:.1f}MB, over the {MAX_BLOB_BYTES / 1e6:.0f}MB "
            f"lexicon limit. Resize before uploading."
        )

    mime = MIME_BY_SUFFIX.get(path.suffix.lower())
    if not mime:
        sys.exit(f"{path.name}: only {', '.join(sorted(MIME_BY_SUFFIX))} are accepted.")

    resp = httpx.post(
        f"{pds}/xrpc/com.atproto.repo.uploadBlob",
        content=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
        timeout=120,
    )
    resp.raise_for_status()
    blob = resp.json()["blob"]

    dims = image_dimensions(data)
    print(f"  uploaded {path.name} ({len(data) / 1e3:.0f}KB, {mime}) -> {blob['ref']['$link']}")
    return {"blob": blob, "dims": dims}


def create_record(pds: str, token: str, did: str, record: dict) -> dict:
    resp = httpx.post(
        f"{pds}/xrpc/com.atproto.repo.createRecord",
        json={"repo": did, "collection": LEXICON, "record": record},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code == 400:
        # Most often lexicon validation: the PDS applies generic checks to
        # unknown collections, so this usually means a malformed blob ref or a
        # field the record schema rejects outright.
        sys.exit(f"Record rejected by PDS: {resp.text}")
    resp.raise_for_status()
    return resp.json()


def list_records(pds: str, did: str) -> list[dict]:
    resp = httpx.get(
        f"{pds}/xrpc/com.atproto.repo.listRecords",
        params={"repo": did, "collection": LEXICON, "limit": 100},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("records", [])


def delete_record(pds: str, token: str, did: str, rkey: str) -> None:
    resp = httpx.post(
        f"{pds}/xrpc/com.atproto.repo.deleteRecord",
        json={"repo": did, "collection": LEXICON, "rkey": rkey},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_record(args, photos: list[dict]) -> dict:
    record = {
        "$type": LEXICON,
        "title": args.title,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    optional = {
        "notes": args.notes,
        "maker": args.maker,
        "material": args.material,
        "lining": args.lining,
        "form": args.form,
        "diameterMm": args.diameter_mm,
        "heightMm": args.height_mm,
        "thicknessMm": args.thickness_mm,
        "weightG": args.weight_g,
        "handleMaterial": args.handle_material,
        "era": args.era,
        "condition": args.condition,
        "restoration": args.restoration,
        "provenance": args.provenance,
    }
    record.update({k: v for k, v in optional.items() if v is not None})

    if args.tag:
        record["tags"] = args.tag

    if photos:
        record["photos"] = []
        for i, p in enumerate(photos):
            entry = {
                "image": p["blob"],
                "alt": args.alt[i] if i < len(args.alt) else f"{args.title}, photo {i + 1}",
            }
            if p["dims"]:
                entry["aspectRatio"] = {"width": p["dims"][0], "height": p["dims"][1]}
            record["photos"].append(entry)

    return record


def main() -> None:
    ap = argparse.ArgumentParser(description="Publish a pan record to your ATProto repo")

    ap.add_argument("--title", help="Required unless --list or --delete.")
    ap.add_argument("--photo", type=Path, action="append", default=[],
                    help="Photo file. Repeat for multiple, up to 8.")
    ap.add_argument("--alt", action="append", default=[],
                    help="Alt text, positionally matched to --photo. Required by the lexicon; "
                         "a generic one is generated if omitted.")

    ap.add_argument("--notes")
    ap.add_argument("--maker")
    ap.add_argument("--material", choices=["copper", "brass", "bronze", "unknown"])
    ap.add_argument("--lining", choices=["tin", "silver", "stainless", "nickel", "unlined", "unknown"])
    ap.add_argument("--form", choices=[
        "saucepan", "saucier", "sauteuse", "saute", "skillet", "rondeau", "windsor",
        "bain-marie", "jam-pan", "fish-kettle", "stockpot", "gratin", "roasting-pan",
        "egg-bowl", "mold", "other",
    ])
    ap.add_argument("--diameter-mm", type=int)
    ap.add_argument("--height-mm", type=int)
    ap.add_argument("--thickness-mm", type=float)
    ap.add_argument("--weight-g", type=int)
    ap.add_argument("--handle-material", choices=[
        "cast-iron", "brass", "bronze", "stainless", "wood", "copper", "other", "unknown",
    ])
    ap.add_argument("--era")
    ap.add_argument("--condition", choices=[
        "unrestored", "needs-retinning", "retinned", "restored", "working", "display-only",
    ])
    ap.add_argument("--restoration")
    ap.add_argument("--provenance")
    ap.add_argument("--tag", action="append", help="Repeat for multiple tags.")

    ap.add_argument("--pds", default=os.environ.get("ATPROTO_PDS_URL", DEFAULT_PDS))
    ap.add_argument("--handle", default=os.environ.get("BSKY_HANDLE"))
    ap.add_argument("--password", default=os.environ.get("BSKY_APP_PASSWORD"))

    ap.add_argument("--dry-run", action="store_true",
                    help="Build and print the record without uploading or writing.")
    ap.add_argument("--list", action="store_true", help="List pan records in your repo.")
    ap.add_argument("--delete", metavar="RKEY", help="Delete one record by rkey.")

    args = ap.parse_args()

    if args.dry_run and not args.title:
        ap.error("--title is required")

    if args.dry_run:
        # No credentials needed — stub the blob refs so the shape is still visible.
        photos = [
            {
                "blob": {
                    "$type": "blob",
                    "ref": {"$link": f"bafkreiDRYRUN{i}"},
                    "mimeType": MIME_BY_SUFFIX.get(p.suffix.lower(), "image/jpeg"),
                    "size": p.stat().st_size if p.exists() else 0,
                },
                "dims": image_dimensions(p.read_bytes()) if p.exists() else None,
            }
            for i, p in enumerate(args.photo)
        ]
        print(json.dumps(build_record(args, photos), indent=2))
        return

    if not args.handle or not args.password:
        sys.exit("Set BSKY_HANDLE and BSKY_APP_PASSWORD (or pass --handle/--password).")

    pds = args.pds.rstrip("/")
    session = login(pds, args.handle, args.password)
    did, token = session["did"], session["accessJwt"]
    print(f"authenticated as {args.handle} ({did}) on {pds}")

    if args.list:
        records = list_records(pds, did)
        if not records:
            print("no pan records in this repo")
        for r in records:
            rkey = r["uri"].rsplit("/", 1)[-1]
            val = r["value"]
            print(f"  {rkey}  {val.get('title', '(untitled)')}  "
                  f"[{len(val.get('photos', []))} photo(s)]")
        return

    if args.delete:
        delete_record(pds, token, did, args.delete)
        print(f"deleted {args.delete}")
        print("The consumer will revoke its blobs from the allowlist on the next delete event.")
        return

    if not args.title:
        ap.error("--title is required")
    if len(args.photo) > 8:
        ap.error("the lexicon allows at most 8 photos")

    photos = [upload_blob(pds, token, p) for p in args.photo]
    record = build_record(args, photos)
    result = create_record(pds, token, did, record)

    rkey = result["uri"].rsplit("/", 1)[-1]
    print(f"\npublished {result['uri']}")
    print(f"  rkey: {rkey}")
    if photos:
        print("\nImage URLs once the consumer has indexed this record:")
        for p in photos:
            print(f"  /img/{did}:{p['blob']['ref']['$link']}/400")


if __name__ == "__main__":
    main()
