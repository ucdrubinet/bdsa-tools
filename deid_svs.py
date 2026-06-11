#!/usr/bin/env python3
"""
deid_svs.py — standalone Aperio SVS de-identifier.

Distilled from DSA-WSI-DeID (https://github.com/DigitalSlideArchive/DSA-WSI-DeID)
but designed for *minimal copies* and standalone CLI use.

------------------------------------------------------------
Why this exists
------------------------------------------------------------
The DSA pipeline shuttles each slide through:
  import_dir -> Girder assetstore -> working_folder -> tempdir -> approved -> export
That's 4-5 full-size copies per slide, which is brutal for multi-GB WSIs.

This script does:
  input.svs --> output.svs        (one stream-copy)

The trick: `tifftools.write_tiff` reads pixel-data byte ranges directly from
the source file via the offsets stored in each IFD's tags.  We mutate
*metadata IFDs in memory* (descriptions, dates, label/macro entries) but
never decode or re-encode the pixel pyramid.  The only "extra copy" is a
~50 KB tempfile if you ask for a synthetic black-with-title label.

------------------------------------------------------------
What it removes (out of the box)
------------------------------------------------------------
By default the config strips everything DSA-WSI-DeID's own redaction code
strips automatically, plus the label image (DSA only removes the label
when always_redact_label=True; we do it by default since that's the
common reason to run a deid script).

  - Label image (the printed barcode/handwriting — main PHI surface)
  - Aperio ImageDescription fields:
      Filename, ImageID, Title    -> replaced with new title
      DSR ID, Time, Time Zone, User -> removed
      Date (MM/DD/YYYY)           -> 01/01/YYYY (year preserved)
  - TIFF tags:
      DateTime (YYYY:MM:DD ...)   -> YYYY:01:01 ... (year preserved)
      Software                    -> redaction marker
      Copyright, HostComputer     -> removed
      DocumentName, NDPI_REFERENCE -> replaced with new title

Edit deid_config.yaml to change any of this.  See `--dump-config`.

------------------------------------------------------------
Usage
------------------------------------------------------------
  # Single file, default config
  python deid_svs.py input.svs output.svs

  # Whole directory (mirrors structure under --output-dir)
  python deid_svs.py --input-dir slides/ --output-dir slides_deid/

  # Custom config + custom title
  python deid_svs.py input.svs output.svs \\
      --config my_config.yaml --title "STUDY-001"

  # Dry run (parse, plan, but write nothing)
  python deid_svs.py input.svs output.svs --dry-run

  # Regenerate the default config file
  python deid_svs.py --dump-config deid_config.yaml
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tifftools
from PIL import Image, ImageColor, ImageDraw, ImageFont

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc


logger = logging.getLogger("deid_svs")


# ---------------------------------------------------------------------------
# Default configuration (kept in sync with deid_config.yaml)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: dict = {
    "images": {
        "remove_label": True,
        "remove_macro": False,
        "remove_thumbnail": False,
        "replace_label_with_title": True,
        "title_label_min_width": 384,
        "title_label_bg": "#000000",
        "title_label_fg": "#ffffff",
    },
    "aperio_metadata": {
        "replace_with_title": ["Filename", "ImageID", "Title"],
        "remove": ["DSR ID", "Time", "Time Zone", "User"],
        "redact_date_keep_year_only": True,
    },
    "tiff_tags": {
        "remove": ["Copyright", "HostComputer"],
        "replace_with_title": ["DocumentName", "NDPI_REFERENCE"],
        "replace_software": True,
        "redact_datetime_keep_year_only": True,
    },
    "naming": {
        "new_title_template": "{stem}",
    },
}

SOFTWARE_MARKER = "DSA-style Redaction (standalone deid_svs.py)"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge override into base; override wins on leaf values."""
    out = deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[Path]) -> dict:
    if path is None:
        return deepcopy(DEFAULT_CONFIG)
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    return deep_merge(DEFAULT_CONFIG, user)


def render_title(template: Optional[str], input_path: Path) -> str:
    if not template:
        template = "{stem}"
    return template.format(stem=input_path.stem, name=input_path.name)


# ---------------------------------------------------------------------------
# Aperio ImageDescription parsing
# ---------------------------------------------------------------------------
# An Aperio SVS main-image ImageDescription looks like:
#   Aperio Image Library v12.0.15
#   159996x66816 [0,0 159996x66816] (256x256) JPEG/RGB Q=70|<key> = <val>|...
#
# i.e. a multi-line "header" (separated by '\n') followed by '|'-delimited
# key=value pairs.  The header uses '\n' internally; the first '|' is the
# boundary between header and key-value section.
def parse_aperio_image_description(desc: str) -> tuple[str, dict[str, str]]:
    parts = desc.split("|")
    header = parts[0]
    fields: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            k, _, v = part.partition("=")
            fields[k.strip()] = v.strip()
    return header, fields


def build_aperio_image_description(header: str, fields: dict[str, str]) -> str:
    return "|".join([header] + [f"{k} = {v}" for k, v in sorted(fields.items())])


# ---------------------------------------------------------------------------
# IFD classification (Aperio convention)
# ---------------------------------------------------------------------------
# We classify by walking the IFD list from the end backwards.  Aperio puts
# the label and macro in a contiguous block at the end of the file; pyramid
# levels live before them.  As soon as we hit a non-label/non-macro IFD
# walking back, we stop — that boundary is the start of the pyramid.
#
# This is *safer* than DSA's per-IFD NewSubfileType=1 fallback, which can
# misclassify pyramid levels as 'label' on files that don't follow the
# spec exactly.  We trust the ImageDescription text + NewSubfileType=9
# (which is unique to macros) and don't fall back to NewSubfileType=1.
def _associated_kind_from_description(ifd: dict) -> Optional[str]:
    """If the description's inner line starts with 'label' or 'macro' (etc.),
    return that lowercase token.  Otherwise None.
    """
    desc = ifd["tags"].get(tifftools.Tag.ImageDescription.value, {}).get("data", "")
    if not desc:
        return None
    normalized = desc.replace("\r", "\n").replace("\n\n", "\n")
    inner = normalized.split("\n", 1)[-1].strip()
    if not inner:
        return None
    tokens = inner.split()
    if tokens and not tokens[0][:1].isdigit():
        return tokens[0].lower()
    return None


@dataclass
class IFDClassification:
    label_indices: list[int] = field(default_factory=list)
    macro_indices: list[int] = field(default_factory=list)
    thumbnail_idx: Optional[int] = None
    pyramid_boundary: int = 0   # smallest index >= which is associated


def classify_aperio_ifds(ifds: list[dict]) -> IFDClassification:
    """Walk from the end backwards to find label/macro; the first IFD that
    doesn't look like an associated image is the boundary."""
    cls = IFDClassification(pyramid_boundary=len(ifds))
    for idx in range(len(ifds) - 1, 0, -1):
        kind = _associated_kind_from_description(ifds[idx])
        if kind == "label":
            cls.label_indices.append(idx)
            cls.pyramid_boundary = idx
            continue
        if kind == "macro":
            cls.macro_indices.append(idx)
            cls.pyramid_boundary = idx
            continue
        # Last-resort: NewSubfileType=9 is unique to Aperio macro images.
        sub_tag = ifds[idx]["tags"].get(tifftools.Tag.NewSubfileType.value)
        if sub_tag and sub_tag["data"][0] == 9:
            cls.macro_indices.append(idx)
            cls.pyramid_boundary = idx
            continue
        break  # hit the pyramid; stop walking back

    # Thumbnail heuristic: IFD 1 with the ReducedImage bit set, that lives
    # *before* the associated-image block, and isn't itself the start of
    # that block.
    if len(ifds) >= 2 and cls.pyramid_boundary > 1:
        sub_tag = ifds[1]["tags"].get(tifftools.Tag.NewSubfileType.value)
        if sub_tag:
            reduced_bit = tifftools.Tag.NewSubfileType.bitfield.ReducedImage.value
            if sub_tag["data"][0] & reduced_bit:
                cls.thumbnail_idx = 1
    return cls


# ---------------------------------------------------------------------------
# Synthetic title-label image
# ---------------------------------------------------------------------------
def render_title_label_image(
    title: str,
    min_width: int,
    bg: str,
    fg: str,
) -> Image.Image:
    """Black square image with the title centered (DSA-WSI-DeID style)."""
    mode = "RGB"
    bg_color = ImageColor.getcolor(bg, mode)
    fg_color = ImageColor.getcolor(fg, mode)

    target_w = max(min_width, 0)

    # Iteratively size the font so the title spans 85-95% of the width.
    font = None
    font_size = max(int(0.15 * target_w), 12)
    for _ in range(3):
        font = _load_font(font_size)
        bbox = font.getbbox(title)
        text_w = bbox[2] - bbox[0]
        if text_w == 0:
            break
        if 0.85 * target_w <= text_w <= 0.95 * target_w:
            break
        font_size = max(int(font_size * target_w * 0.9 / text_w), 8)
    if font is None:
        font = ImageFont.load_default()

    bbox = font.getbbox(title)
    text_w = max(bbox[2] - bbox[0], 1)
    text_h = max(bbox[3] - bbox[1], 1)
    title_h = int(text_h * 1.25)

    # Square output (matches DSA's default).
    side = max(target_w, title_h)
    img = Image.new(mode, (side, side), color=bg_color)
    draw = ImageDraw.Draw(img)
    draw.text(
        ((side - text_w) // 2 - bbox[0], (side - text_h) // 2 - bbox[1]),
        title,
        fill=fg_color,
        font=font,
    )
    return img


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size=size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)  # PIL >= 10.1
    except (TypeError, OSError):
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Core: redact one SVS in place (input -> output, single stream copy)
# ---------------------------------------------------------------------------
@dataclass
class RedactionResult:
    input: Path
    output: Path
    title: str
    label_removed: bool = False
    macro_removed: bool = False
    thumbnail_removed: bool = False
    label_replaced_with_title: bool = False
    aperio_keys_replaced: list[str] = field(default_factory=list)
    aperio_keys_removed: list[str] = field(default_factory=list)
    tiff_tags_removed: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def deid_svs_file(
    input_path: Path,
    output_path: Path,
    config: dict,
    title_override: Optional[str] = None,
    dry_run: bool = False,
) -> RedactionResult:
    """De-identify a single SVS file.  Returns a record of what changed."""
    t0 = time.time()
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()

    if input_path == output_path:
        raise ValueError(
            f"Input and output paths are the same ({input_path}); "
            "refusing to overwrite the source slide."
        )

    info = tifftools.read_tiff(str(input_path))
    ifds = info["ifds"]
    if not ifds:
        raise ValueError(f"No IFDs found in {input_path}; not a TIFF?")

    title = title_override or render_title(
        config["naming"]["new_title_template"], input_path
    )
    result = RedactionResult(input=input_path, output=output_path, title=title)

    # ---------------------------------------------------------------- 1. Aperio metadata
    main_desc_tag = ifds[0]["tags"].get(tifftools.Tag.ImageDescription.value)
    if main_desc_tag is None:
        raise ValueError(
            f"{input_path} has no ImageDescription on IFD 0 — not an Aperio SVS?"
        )
    main_desc = main_desc_tag["data"]
    header, fields = parse_aperio_image_description(main_desc)

    aperio_cfg = config["aperio_metadata"]
    for k in aperio_cfg.get("replace_with_title", []):
        if k in fields:
            fields[k] = title
            result.aperio_keys_replaced.append(k)
        else:
            # add it anyway so the new title is recorded
            fields[k] = title
            result.aperio_keys_replaced.append(k)
    for k in aperio_cfg.get("remove", []):
        if fields.pop(k, None) is not None:
            result.aperio_keys_removed.append(k)
    if aperio_cfg.get("redact_date_keep_year_only", True):
        date = fields.get("Date")
        if date and len(date) >= 10:
            # Aperio Date format is "MM/DD/YYYY"
            fields["Date"] = "01/01/" + date[6:10]

    new_main_desc = build_aperio_image_description(header, fields)
    main_desc_tag["data"] = new_main_desc

    # ---------------------------------------------------------------- 2. Classify & remove IFDs
    img_cfg = config["images"]
    cls = classify_aperio_ifds(ifds)
    indices_to_remove: list[int] = []

    if cls.label_indices and img_cfg["remove_label"]:
        indices_to_remove.extend(cls.label_indices)
        result.label_removed = True
    if cls.macro_indices and img_cfg["remove_macro"]:
        indices_to_remove.extend(cls.macro_indices)
        result.macro_removed = True
    if cls.thumbnail_idx is not None and img_cfg["remove_thumbnail"]:
        indices_to_remove.append(cls.thumbnail_idx)
        result.thumbnail_removed = True

    # Update the per-IFD ImageDescription on every IFD that survives:
    #   - main IFD already updated above with the redacted fields.
    #   - thumbnail / pyramid: keep the IFD's own first '|' segment (which
    #     carries its own resolution summary) and graft on the redacted
    #     fields from the main image.
    #   - label / macro: their description format is different and they
    #     don't carry key=value pairs; leave them alone (they'll get
    #     popped if marked, otherwise kept verbatim).
    associated_set = set(cls.label_indices) | set(cls.macro_indices)
    for idx in range(1, len(ifds)):
        if idx in associated_set:
            continue
        ifd_desc_tag = ifds[idx]["tags"].get(tifftools.Tag.ImageDescription.value)
        if ifd_desc_tag and ifd_desc_tag.get("data"):
            ifd_header = ifd_desc_tag["data"].split("|", 1)[0]
            ifd_desc_tag["data"] = build_aperio_image_description(ifd_header, fields)

    # Remove in reverse order so indices stay valid.
    for idx in sorted(set(indices_to_remove), reverse=True):
        ifds.pop(idx)

    # ---------------------------------------------------------------- 3. TIFF-tag scrub
    tiff_cfg = config["tiff_tags"]
    for ifd in ifds:
        tags = ifd["tags"]
        for name in tiff_cfg.get("remove", []):
            tag_obj = getattr(tifftools.Tag, name, None)
            if tag_obj is not None and tag_obj.value in tags:
                del tags[tag_obj.value]
                if name not in result.tiff_tags_removed:
                    result.tiff_tags_removed.append(name)
        for name in tiff_cfg.get("replace_with_title", []):
            tag_obj = getattr(tifftools.Tag, name, None)
            if tag_obj is not None and tag_obj.value in tags:
                tags[tag_obj.value] = {
                    "datatype": tifftools.Datatype.ASCII,
                    "data": title,
                }
        if tiff_cfg.get("replace_software", True):
            tags[tifftools.Tag.Software.value] = {
                "datatype": tifftools.Datatype.ASCII,
                "data": SOFTWARE_MARKER,
            }
        if tiff_cfg.get("redact_datetime_keep_year_only", True):
            dt_tag = tifftools.Tag.DateTime.value
            if dt_tag in tags:
                val = tags[dt_tag]["data"]
                if len(val) >= 10:
                    tags[dt_tag]["data"] = val[:5] + "01:01" + val[10:]
                else:
                    del tags[dt_tag]

    # ---------------------------------------------------------------- 4. Write output
    # We keep the synthetic-label tempfile alive through write_tiff because
    # the new label IFD's pixel-data offsets point into it.
    with tempfile.TemporaryDirectory(prefix="deid_svs_") as tempdir:
        if (
            img_cfg.get("replace_label_with_title", True)
            and result.label_removed
        ):
            label_img = render_title_label_image(
                title=title,
                min_width=int(img_cfg["title_label_min_width"]),
                bg=img_cfg["title_label_bg"],
                fg=img_cfg["title_label_fg"],
            )
            label_path = Path(tempdir) / "label.tiff"
            label_img.save(
                label_path, format="tiff", compression="jpeg", quality=90
            )

            label_info = tifftools.read_tiff(str(label_path))
            label_ifd = label_info["ifds"][0]
            # Mark this as a label associated image (Aperio convention).
            label_ifd["tags"][tifftools.Tag.NewSubfileType.value] = {
                "datatype": tifftools.Datatype.LONG,
                "data": [1],
            }
            # Aperio expects ImageDescription beginning with the original
            # main-image's resolution line, then a "label W x H" line.
            try:
                resolution_line = header.split("\n", 1)[1]
            except IndexError:
                resolution_line = header
            label_ifd["tags"][tifftools.Tag.ImageDescription.value] = {
                "datatype": tifftools.Datatype.ASCII,
                "data": (
                    f"{resolution_line}\nlabel "
                    f"{label_img.width}x{label_img.height}"
                ),
            }
            ifds.append(label_ifd)
            result.label_replaced_with_title = True

        if dry_run:
            logger.info("[dry-run] Would write %s", output_path)
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tifftools.write_tiff(ifds, str(output_path), allowExisting=True)

    result.elapsed_seconds = time.time() - t0
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def discover_svs_files(input_dir: Path) -> list[Path]:
    return sorted(p for p in input_dir.rglob("*") if p.suffix.lower() == ".svs")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="De-identify Aperio SVS files (single-pass, no extra copies).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="Input .svs file (or omit and use --input-dir)")
    parser.add_argument("output", nargs="?", help="Output .svs file (omit when using --input-dir)")
    parser.add_argument("--input-dir", type=Path, help="Process all .svs files under this directory.")
    parser.add_argument("--output-dir", type=Path, help="Mirror --input-dir's structure under this directory.")
    parser.add_argument("--config", type=Path, help="YAML config to override defaults.")
    parser.add_argument("--title", help="Override the new title for the output (single-file mode only).")
    parser.add_argument("--dry-run", action="store_true", help="Plan and report; don't write any output.")
    parser.add_argument("--dump-config", type=Path, help="Write the default config to this YAML path and exit.")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting existing output files.")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if args.dump_config:
        args.dump_config.parent.mkdir(parents=True, exist_ok=True)
        with open(args.dump_config, "w") as f:
            yaml.safe_dump(DEFAULT_CONFIG, f, sort_keys=False)
        logger.info("Wrote default config to %s", args.dump_config)
        return 0

    config = load_config(args.config)

    # Build the list of (input, output) jobs.
    jobs: list[tuple[Path, Path]] = []
    if args.input_dir:
        if not args.output_dir:
            parser.error("--input-dir requires --output-dir")
        if args.input or args.output or args.title:
            parser.error("--input-dir is incompatible with positional input/output and --title")
        for src in discover_svs_files(args.input_dir):
            rel = src.relative_to(args.input_dir)
            jobs.append((src, args.output_dir / rel))
        if not jobs:
            logger.error("No .svs files found under %s", args.input_dir)
            return 1
    else:
        if not (args.input and args.output):
            parser.error("Provide either positional INPUT OUTPUT or --input-dir/--output-dir")
        jobs.append((Path(args.input), Path(args.output)))

    failures = 0
    for src, dst in jobs:
        if dst.exists() and not args.overwrite and not args.dry_run:
            logger.warning("Skipping (exists, no --overwrite): %s", dst)
            continue
        try:
            res = deid_svs_file(
                input_path=src,
                output_path=dst,
                config=config,
                title_override=args.title,
                dry_run=args.dry_run,
            )
            logger.info(
                "%s -> %s  (title=%r  label=%s  macro=%s  thumb=%s  %.2fs)",
                src.name,
                dst,
                res.title,
                res.label_removed,
                res.macro_removed,
                res.thumbnail_removed,
                res.elapsed_seconds,
            )
        except Exception:
            logger.exception("Failed: %s", src)
            failures += 1

    if failures:
        logger.error("%d / %d files failed", failures, len(jobs))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
