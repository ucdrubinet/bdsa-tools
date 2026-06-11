# remove_czi_labels.py

Removes `Label` attachments from Zeiss CZI files — a de-identification step for WSI pipelines. CZI files can embed a macro image of the physical slide that may contain patient or sample identifiers. This script strips those attachments directly from the binary format, with no Zeiss Zen dependency.

- **Non-destructive**: outputs `<original_name>_LabelRemoved.czi`, originals untouched
- **No dependencies**: standard library only (`struct`, `shutil`, `glob`, `os`, `sys`)
- **Re-run safe**: files already containing `_LabelRemoved` in their name are skipped automatically

---

## Running the Script

**CLI:**
```bash
python remove_czi_labels.py /path/to/czi/folder
python remove_czi_labels.py        # interactive folder prompt
```

**Programmatic:**
```python
from remove_czi_labels import remove_label

remove_label("slide_001.czi")                        # default output name
remove_label("slide_001.czi", dst_path="clean.czi")  # custom output path
# Returns True if a Label was removed, False if skipped
```

**Sample output:**
```
Found 3 CZI file(s) in /data/slides

  [done] Removed 1 Label attachment(s) -> slide_001_LabelRemoved.czi
  [skip] No Label attachment found: slide_002.czi
  [done] Removed 1 Label attachment(s) -> slide_003_LabelRemoved.czi

Done. 2 processed, 1 skipped.
```

---

## How It Works

### CZI File Structure

A CZI file is a sequence of typed **segments**, each starting with a 32-byte header. The script navigates three of them:

```
[ZISRAWFILE]       ← always at byte 0; payload contains a pointer to the attachment directory
[...]              ← image data, metadata, etc.
[ZISRAWATTDIR]     ← attachment directory; lists all attachments by name
[ZISRAWATT × N]    ← actual attachment data (label image bytes live here)
```

The script reads the pointer in `ZISRAWFILE`, jumps to `ZISRAWATTDIR`, filters out any entry named `"Label"`, and rewrites the directory in the copied file.

---

### Format Constants

#### Segment header — `SEGMENT_HEADER_FMT = "<16sqq"` (32 bytes)

Every segment opens with this structure:

```
 0          16          24          32
 | id (16s) | alloc (q) | used (q)  |
```

| Field | Type | Description |
|---|---|---|
| `id` | `16s` | ASCII segment type, e.g. `ZISRAWFILE`, `ZISRAWATTDIR` |
| `alloc` | `int64` | Bytes allocated for the payload on disk |
| `used` | `int64` | Bytes actually written |

`<` = little-endian. `SEGMENT_HEADER_SIZE = 32` is computed via `struct.calcsize()`.

---

#### File header pointer — `ATT_DIR_POSITION_OFFSET = 72`

The `ZISRAWFILE` payload is 512 bytes (`FILE_HEADER_DATA_SIZE`). At **byte 72**, an `int64` stores the file offset of the `ZISRAWATTDIR` segment. A value of `0` means no attachments exist.

```
ZISRAWFILE payload:
 0      ...      72       80      ...     512
          | int64: AttDir offset |
```

---

#### Attachment directory — `ATT_DIR_HEADER_SIZE = 256`

Immediately after the segment header, `ZISRAWATTDIR` begins with:

| Field | Size | Description |
|---|---|---|
| `entry_count` | 4 bytes | Number of attachment entries that follow |
| reserved | 252 bytes | Zeroed; reserved by spec |

---

#### Attachment entry — `ATTACHMENT_ENTRY_SIZE = 128`

Each attachment is a 128-byte `AttachmentEntryA1` record. The **Name** field (checked against `"Label"`) sits at byte 48 and is 80 bytes long (`ATTACHMENT_NAME_OFFSET = 48`, `ATTACHMENT_NAME_LENGTH = 80`):

```
AttachmentEntryA1 (128 bytes):
 0         48                  128
 | misc(48) | Name (80 bytes)  |
```

---

### Patch Strategy

1. `shutil.copy2` duplicates the original to `_LabelRemoved.czi`
2. The copy is opened in `r+b` mode and seeked to the `ZISRAWATTDIR` payload
3. Only three things are rewritten: `entry_count`, retained entries, zeroed bytes for removed slots
4. Everything else (including the actual label image bytes elsewhere in the file) is untouched

This avoids shifting any file offsets, which would risk corrupting the file.