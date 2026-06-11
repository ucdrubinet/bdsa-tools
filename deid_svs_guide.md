# deid_svs.py — Aperio SVS De-identification Script

**Documentation covering usage, configuration, and code internals**

---

## Table of Contents

1. [Overview](#overview)
2. [How to Use the Script](#how-to-use-the-script)
   - [Requirements](#requirements)
   - [Installation](#installation)
   - [Quick Start](#quick-start)
   - [Single File Mode](#single-file-mode)
   - [Batch Directory Mode](#batch-directory-mode)
   - [Using a Config File](#using-a-config-file)
   - [Dry Run Mode](#dry-run-mode)
   - [All CLI Flags Reference](#all-cli-flags-reference)
   - [Understanding the Log Output](#understanding-the-log-output)
   - [Post-Run Security Checklist](#post-run-security-checklist)
3. [Configuration Reference](#configuration-reference)
   - [images](#images)
   - [aperio_metadata](#aperio_metadata)
   - [tiff_tags](#tiff_tags)
   - [naming](#naming)
4. [Code Walkthrough](#code-walkthrough)
   - [Architecture Overview](#architecture-overview)
   - [Config Loading](#config-loading)
   - [Aperio Metadata Parsing](#aperio-metadata-parsing)
   - [IFD Classification](#ifd-classification)
   - [Synthetic Label Generation](#synthetic-label-generation)
   - [Core De-identification Function](#core-de-identification-function)
   - [CLI Entry Point](#cli-entry-point)

---

## Overview

`didsvs.py` is a standalone command-line tool for removing Protected Health Information (PHI) from Aperio SVS whole-slide image files. It is derived from [DSA-WSI-DeID](https://github.com/DigitalSlideArchive/DSA-WSI-DeID) but redesigned for direct local use without a Girder/DSA server.

The core design principle: **one stream copy, no pixel re-encoding.** The script mutates metadata structures in memory, then writes a clean output file using byte-range references to the original pixel pyramid. The input file is never modified.

**What it removes by default:**

| PHI Surface | Action |
|---|---|
| Label image (barcode, handwriting) | Removed; replaced with synthetic black title image |
| Aperio fields: `Filename`, `ImageID`, `Title` | Replaced with new safe title |
| Aperio fields: `DSR ID`, `Time`, `Time Zone`, `User` | Removed entirely |
| Aperio `Date` field (`MM/DD/YYYY`) | Truncated to `01/01/YYYY` |
| TIFF `DateTime` tag | Truncated to `YYYY:01:01 00:00:00` |
| TIFF `Software` tag | Overwritten with redaction marker |
| TIFF `Copyright`, `HostComputer` tags | Removed entirely |
| TIFF `DocumentName`, `NDPI_REFERENCE` tags | Replaced with new safe title |

---

## How to Use the Script

### Requirements

- Python 3.8+
- The following Python packages:

```
tifftools
Pillow
PyYAML
```

### Installation

Install dependencies with pip:

```bash
pip install tifftools Pillow pyyaml
```

Verify:

```bash
python didsvs.py --help
```

---

### Quick Start

De-identify a single SVS file with all defaults:

```bash
python didsvs.py input.svs output.svs
```

The new title will default to the input filename stem (e.g. `input.svs` becomes title `input`).

---

### Single File Mode

```bash
python didsvs.py <input.svs> <output.svs> [options]
```

**Example: basic de-identification**

```bash
python didsvs.py /data/phi/WSI_123.svs /data/deid/WSI_123.svs
```

**Example: override the title manually**

```bash
python didsvs.py /data/phi/WSI_123.svs /data/deid/WSI_123.svs --title "STUDY-001"
```

All identifying fields (`Filename`, `ImageID`, `Title`, `DocumentName`, etc.) will be set to `STUDY-001` instead of the filename stem.

**Example: use a custom config**

```bash
python didsvs.py input.svs output.svs --config my_config.yaml
```

**Example: allow overwriting an existing output file**

```bash
python didsvs.py input.svs output.svs --overwrite
```

By default, if `output.svs` already exists the script will skip it and log a warning.

---

### Batch Directory Mode

Process an entire directory of SVS files recursively:

```bash
python didsvs.py --input-dir <input_dir> --output-dir <output_dir> [options]
```

The output directory mirrors the input directory structure. Non-SVS files are ignored.

**Example:**

```bash
python didsvs.py --input-dir /data/phi/ --output-dir /data/deid/
```

If `/data/phi/` contains:

```
/data/phi/batch1/slide_A.svs
/data/phi/batch1/slide_B.svs
/data/phi/batch2/slide_C.svs
```

The output will be:

```
/data/deid/batch1/slide_A.svs
/data/deid/batch1/slide_B.svs
/data/deid/batch2/slide_C.svs
```

**Notes on batch mode:**
- `--title` is not available in batch mode (each file uses its own stem as title).
- `--overwrite` applies to all files in the batch.
- If any individual file fails, the script logs the error and continues; it reports a non-zero exit code at the end.

---

### Using a Config File

To customize de-identification behavior, use a YAML config file.

**Step 1: Generate the default config**

```bash
python didsvs.py --dump-config my_config.yaml
```

This writes the full default config to `my_config.yaml`. Open and edit it.

**Step 2: Edit what you need**

For example, to also remove the macro image, change:

```yaml
images:
  remove_macro: false   # default
```

to:

```yaml
images:
  remove_macro: true
```

**Step 3: Run with your config**

```bash
python didsvs.py input.svs output.svs --config my_config.yaml
```

The config deep-merges with the built-in defaults, so you only need to include the keys you want to override. A minimal config file is valid.

---

### Dry Run Mode

Use `--dry-run` to parse the file and plan all changes without writing any output:

```bash
python didsvs.py input.svs output.svs --dry-run
```

This is useful for:
- Verifying the script can parse a file before running a large batch
- Previewing which fields will be modified
- Checking title resolution without committing output

The log output will show the same information as a real run but with `[dry-run]` markers.

---

### All CLI Flags Reference

| Flag | Description |
|---|---|
| `input` | Positional. Path to input `.svs` file. |
| `output` | Positional. Path to output `.svs` file. |
| `--input-dir PATH` | Process all `.svs` files under this directory. |
| `--output-dir PATH` | Write outputs mirroring `--input-dir` structure. Required with `--input-dir`. |
| `--config PATH` | Path to YAML config file. Deep-merges with built-in defaults. |
| `--title TEXT` | Override the new title string. Single-file mode only. |
| `--dry-run` | Parse and plan without writing any output. |
| `--dump-config PATH` | Write the default config YAML to `PATH` and exit. |
| `--overwrite` | Allow overwriting existing output files. Without this, existing outputs are skipped. |
| `-v`, `--verbose` | Enable `DEBUG`-level log output. |

---

### Understanding the Log Output

A successful run logs one line per file:

```
2024-08-01 14:32:01 INFO    WSI_123.svs -> /data/deid/WSI_123.svs  (title='WSI_123'  label=True  macro=False  thumb=False  3.47s)
```

| Field | Meaning |
|---|---|
| `title='WSI_123'` | The new title written to all identifying metadata fields |
| `label=True` | The label image was removed (and replaced if configured) |
| `macro=False` | The macro image was kept |
| `thumb=False` | The thumbnail IFD was kept |
| `3.47s` | Wall-clock time for this file |

Use `-v` to also see which specific Aperio keys and TIFF tags were modified.

---

### Post-Run Security Checklist

The output file is clean; the input file still contains all original PHI. After a successful run:

- [ ] Verify the output `.svs` opens correctly in your viewer
- [ ] Confirm the label image shows the new title, not original barcodes
- [ ] Securely delete input files from any **intermediate/scratch locations** (e.g. local disk, `/tmp`) using `shred` or equivalent
- [ ] Inputs on PHI-controlled storage (e.g. UCDMC SFTP) should remain there and follow your institutional data retention policy
- [ ] Do **not** store any mapping between old filenames and new titles next to the de-identified files

---

## Configuration Reference

The config file has four top-level sections. Run `python didsvs.py --dump-config out.yaml` to generate the file with inline comments.

---

### `images`

Controls which embedded image layers are removed.

```yaml
images:
  remove_label: true
  remove_macro: false
  remove_thumbnail: false
  replace_label_with_title: true
  title_label_min_width: 384
  title_label_bg: "#000000"
  title_label_fg: "#ffffff"
```

| Key | Default | Description |
|---|---|---|
| `remove_label` | `true` | Removes the label IFD (barcode + handwriting; highest PHI surface). |
| `remove_macro` | `false` | Removes the macro image (low-res whole-slide thumbnail including label edges). |
| `remove_thumbnail` | `false` | Removes the thumbnail IFD (small downsampled main image; rarely contains PHI). |
| `replace_label_with_title` | `true` | If label was removed, inserts a synthetic black image with the new title stamped in white. Keeps downstream tools that expect a label from breaking. |
| `title_label_min_width` | `384` | Minimum pixel width of the synthetic label image. |
| `title_label_bg` | `"#000000"` | Background color of the synthetic label. |
| `title_label_fg` | `"#ffffff"` | Text color of the synthetic label. |

---

### `aperio_metadata`

Controls the `key=value` fields in the Aperio `ImageDescription` tag (stored as `|`-separated pairs on IFD 0).

```yaml
aperio_metadata:
  replace_with_title:
    - Filename
    - ImageID
    - Title
  remove:
    - DSR ID
    - Time
    - Time Zone
    - User
  redact_date_keep_year_only: true
```

| Key | Description |
|---|---|
| `replace_with_title` | List of Aperio keys whose values are replaced with the new title string. Keys are added even if absent in the source file. |
| `remove` | List of Aperio keys to delete entirely. Missing keys are silently ignored. |
| `redact_date_keep_year_only` | If `true`, converts `Date` from `MM/DD/YYYY` to `01/01/YYYY`. |

---

### `tiff_tags`

Controls standard TIFF tags applied to **every IFD** in the output.

```yaml
tiff_tags:
  remove:
    - Copyright
    - HostComputer
  replace_with_title:
    - DocumentName
    - NDPI_REFERENCE
  replace_software: true
  redact_datetime_keep_year_only: true
```

| Key | Description |
|---|---|
| `remove` | TIFF tag names to delete. Must match `tifftools.Tag` names exactly (case-sensitive). |
| `replace_with_title` | TIFF tag names whose values are replaced with the new title string. |
| `replace_software` | If `true`, overwrites the `Software` tag with the string `DSA-style Redaction (standalone deid_svs.py)`. |
| `redact_datetime_keep_year_only` | If `true`, truncates `DateTime` from `YYYY:MM:DD HH:MM:SS` to `YYYY:01:01 00:00:00`. |

---

### `naming`

Controls how the new title is derived from the input filename.

```yaml
naming:
  new_title_template: "{stem}"
```

| Placeholder | Expands to |
|---|---|
| `{stem}` | Filename without extension (e.g. `WSI_123` from `WSI_123.svs`) |
| `{name}` | Full filename including extension (e.g. `WSI_123.svs`) |

Use a fixed template if you want all files in a batch to share a prefix:

```yaml
naming:
  new_title_template: "STUDY_2024_{stem}"
```

This would produce titles like `STUDY_2024_WSI_123`.

**Important:** Never store the mapping between old filenames and new titles next to the de-identified output files.

---

## Code Walkthrough

### Architecture Overview

The script is structured as five independent layers:

```
CLI (main, discover_svs_files)
    |
    v
Core de-ID function (deid_svs_file)
    |
    +---> Aperio metadata parser (parse/build_aperio_image_description)
    |
    +---> IFD classifier (classify_aperio_ifds)
    |
    +---> Synthetic label renderer (render_title_label_image)
    |
    v
tifftools.write_tiff  <-- writes output (never touches pixel data)
```

The only external I/O happens at the bottom: `tifftools.read_tiff` loads metadata IFDs into memory at the start, and `tifftools.write_tiff` writes the mutated IFDs (with byte-range references to the source pixel data) at the end.

---

### Config Loading

**Functions:** `load_config`, `deep_merge`, `render_title`

```python
DEFAULT_CONFIG: dict = { ... }  # full defaults hardcoded in the script

def load_config(path: Optional[Path]) -> dict:
    if path is None:
        return deepcopy(DEFAULT_CONFIG)
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    return deep_merge(DEFAULT_CONFIG, user)
```

`load_config` reads the user's YAML and calls `deep_merge` to combine it with `DEFAULT_CONFIG`. `deep_merge` is a recursive dictionary merge that walks nested keys: wherever both the base and override have a dict at the same key, it merges them; otherwise the override value wins. This means a user config only needs to contain the keys they want to change.

`render_title` applies the `new_title_template` to the actual input `Path` object. It uses Python's `str.format()` with `{stem}` and `{name}` as the available placeholders.

---

### Aperio Metadata Parsing

**Functions:** `parse_aperio_image_description`, `build_aperio_image_description`

Aperio SVS files store slide metadata as a specially formatted string in the TIFF `ImageDescription` tag on IFD 0. The format is:

```
Aperio Image Library v12.0.15\n
159996x66816 [0,0 159996x66816] (256x256) JPEG/RGB Q=70|Filename = WSI_123|Date = 07/14/2023|User = jsmith|...
```

Everything before the first `|` is the **header** (two lines: library version and resolution summary). Everything after is a `|`-separated list of `Key = Value` pairs.

```python
def parse_aperio_image_description(desc: str) -> tuple[str, dict[str, str]]:
    parts = desc.split("|")
    header = parts[0]
    fields = {}
    for part in parts[1:]:
        if "=" in part:
            k, _, v = part.partition("=")
            fields[k.strip()] = v.strip()
    return header, fields
```

`parse_aperio_image_description` splits on `|`, extracts the header, and builds a `dict` of key-value pairs. The script then mutates this dict (replace, remove, date-redact) and calls `build_aperio_image_description` to reassemble it:

```python
def build_aperio_image_description(header: str, fields: dict[str, str]) -> str:
    return "|".join([header] + [f"{k} = {v}" for k, v in sorted(fields.items())])
```

Note that `sorted()` is applied to the fields, which reorders them alphabetically. This is a minor structural change from the original (Aperio doesn't guarantee field order) but is harmless for downstream parsing.

This same redacted `fields` dict is also grafted onto every **pyramid-level IFD** (IFDs 2, 3, 4, ... which are the downsampled pyramid resolutions). Each pyramid IFD has its own `ImageDescription` with a different resolution summary line but the same key-value section. The script replaces only the key-value section on those IFDs, preserving each IFD's own resolution summary header.

---

### IFD Classification

**Functions:** `classify_aperio_ifds`, `_associated_kind_from_description`, `IFDClassification`

A TIFF file is a list of Image File Directories (IFDs). An Aperio SVS typically arranges them like this:

```
IFD 0   -- full-resolution main image
IFD 1   -- thumbnail (downsampled, marked ReducedImage)
IFD 2   -- pyramid level 1 (1/4 resolution)
IFD 3   -- pyramid level 2 (1/16 resolution)
...
IFD N-2 -- macro image
IFD N-1 -- label image
```

The script needs to identify which IFDs are "associated" (label, macro, thumbnail) so it can selectively remove them.

```python
def _associated_kind_from_description(ifd: dict) -> Optional[str]:
    desc = ifd["tags"].get(tifftools.Tag.ImageDescription.value, {}).get("data", "")
    ...
    tokens = inner.split()
    if tokens and not tokens[0][:1].isdigit():
        return tokens[0].lower()   # returns "label" or "macro"
    return None
```

Associated IFDs have a second line in their `ImageDescription` that starts with a word like `label` or `macro` (not a digit like the resolution line `159996x66816 ...`). `_associated_kind_from_description` extracts that first token.

`classify_aperio_ifds` walks from the **end** of the IFD list backwards:

```python
for idx in range(len(ifds) - 1, 0, -1):
    kind = _associated_kind_from_description(ifds[idx])
    if kind == "label":
        cls.label_indices.append(idx)
    elif kind == "macro":
        cls.macro_indices.append(idx)
    elif sub_tag["data"][0] == 9:    # NewSubfileType=9 = Aperio macro fallback
        cls.macro_indices.append(idx)
    else:
        break  # hit the pyramid; stop
```

It stops as soon as it reaches an IFD that doesn't look like an associated image. This is safer than the DSA approach of checking `NewSubfileType=1` (the generic "reduced resolution" bit), which can misclassify pyramid levels on non-standard files.

The thumbnail is identified separately: IFD 1 with the `ReducedImage` bit set in `NewSubfileType`, appearing before the label/macro block.

---

### Synthetic Label Generation

**Functions:** `render_title_label_image`, `_load_font`

When the label is removed, by default the script inserts a replacement: a solid-color square image with the new title centered in it. This keeps downstream tools (viewers, pipelines) that expect a label IFD from throwing errors.

```python
def render_title_label_image(title, min_width, bg, fg) -> Image.Image:
    ...
    for _ in range(3):
        font = _load_font(font_size)
        bbox = font.getbbox(title)
        text_w = bbox[2] - bbox[0]
        # Iteratively resize font to fill 85-95% of width
        font_size = max(int(font_size * target_w * 0.9 / text_w), 8)
    ...
    img = Image.new("RGB", (side, side), color=bg_color)
    draw = ImageDraw.Draw(img)
    draw.text(...)
    return img
```

The font sizing loop runs up to 3 iterations to find a font size where the rendered title spans 85-95% of the target width. `_load_font` tries a list of known system font paths (Linux DejaVu, macOS Arial, Windows Arial) and falls back to PIL's built-in bitmap font if none are found.

The generated image is saved as a JPEG into a `tempfile.TemporaryDirectory`, then read back via `tifftools.read_tiff` so it becomes a proper tifftools IFD. The script sets its `NewSubfileType` to `1` (associated image) and writes a minimal `ImageDescription` that starts with `label WxH` so Aperio-aware tools recognize it as the label.

The `TemporaryDirectory` stays alive through `write_tiff` because the synthetic label IFD's pixel-data byte offsets point into that temp file. Once `write_tiff` completes, the context manager exits and the tempfile is cleaned up.

---

### Core De-identification Function

**Function:** `deid_svs_file`

This is where all the pieces connect. It takes `input_path`, `output_path`, a `config` dict, and optional `title_override`, and returns a `RedactionResult` dataclass summarizing what changed.

**Step-by-step execution:**

**1. Load IFDs into memory**

```python
info = tifftools.read_tiff(str(input_path))
ifds = info["ifds"]
```

`tifftools.read_tiff` parses all TIFF IFD headers and tag values. Importantly, it does **not** load pixel data into memory; it only records the byte offsets and lengths of each pixel strip/tile. All pixel data remains on disk in the input file.

**2. Scrub Aperio metadata**

Calls `parse_aperio_image_description` on the main IFD's `ImageDescription`, mutates the `fields` dict per the config (replace, remove, date-redact), and writes the rebuilt string back into `main_desc_tag["data"]`. Then propagates the same redacted `fields` to all pyramid IFDs.

**3. Classify and remove associated IFDs**

Calls `classify_aperio_ifds`, builds a list of IFD indices to remove, then pops them in reverse order:

```python
for idx in sorted(set(indices_to_remove), reverse=True):
    ifds.pop(idx)
```

Reverse order is critical: removing a lower-indexed IFD first would shift the indices of everything above it, causing wrong IFDs to be removed.

**4. Scrub TIFF tags**

Iterates over all surviving IFDs and applies the `tiff_tags` config: deletes tags, replaces values with the new title, overwrites `Software`, and redacts `DateTime`.

**5. Write output**

```python
tifftools.write_tiff(ifds, str(output_path), allowExisting=True)
```

`write_tiff` assembles the output file: it writes new IFD headers and tag values from the mutated in-memory structures, and for pixel data strips/tiles it reads the byte ranges directly from the source file using the stored offsets. No pixel decoding or re-encoding happens. The pyramid is reproduced byte-for-byte.

**`RedactionResult`** is a dataclass that records everything changed: booleans for `label_removed`, `macro_removed`, `thumbnail_removed`, `label_replaced_with_title`, plus lists of which Aperio keys and TIFF tags were touched. The CLI logs these fields for each processed file.

---

### CLI Entry Point

**Functions:** `main`, `discover_svs_files`

`main` uses `argparse` to parse arguments, builds a list of `(src, dst)` path tuples (either one pair from positional args, or many pairs by recursively discovering `.svs` files under `--input-dir`), then loops over them calling `deid_svs_file`.

`discover_svs_files` is a one-liner using `Path.rglob("*")` filtered to `.svs` extension:

```python
def discover_svs_files(input_dir: Path) -> list[Path]:
    return sorted(p for p in input_dir.rglob("*") if p.suffix.lower() == ".svs")
```

Errors on individual files are caught with `logger.exception`, the failure count increments, and the loop continues. The final exit code is `0` (all success), `1` (no input files found), or `2` (one or more files failed).

The `--dump-config` path is handled before any file processing: it serializes `DEFAULT_CONFIG` to YAML using `yaml.safe_dump` and exits immediately with code `0`.