"""
Batch remove Label attachments from Zeiss CZI files.

Standalone Python replacement for the Zen Blue macro.
Works directly with the CZI binary segment format — no Zen/ZenPy dependency.

Usage:
    python remove_czi_labels.py /path/to/czi/folder
    python remove_czi_labels.py                        # interactive prompt

Output files are saved as <original_name>_LabelRemoved.czi alongside the originals.
"""

import struct
import shutil
import glob
import sys
import os

# ── CZI format constants ────────────────────────────────────────────────────
SEGMENT_HEADER_FMT = "<16sqq"  # id(16s) + allocated(q) + used(q)
SEGMENT_HEADER_SIZE = struct.calcsize(SEGMENT_HEADER_FMT)  # 32

FILE_HEADER_DATA_SIZE = 512
ATT_DIR_POSITION_OFFSET = 72   # byte offset within FileHeaderSegmentData
ATT_DIR_HEADER_SIZE = 256      # entry_count(4) + reserved(252)

ATTACHMENT_ENTRY_SIZE = 128
ATTACHMENT_NAME_OFFSET = 48    # offset of Name field within AttachmentEntryA1
ATTACHMENT_NAME_LENGTH = 80


def _read_segment_header(f):
    """Read a 32-byte segment header. Returns (id_str, allocated, used) or None at EOF."""
    raw = f.read(SEGMENT_HEADER_SIZE)
    if len(raw) < SEGMENT_HEADER_SIZE:
        return None
    seg_id, allocated, used = struct.unpack(SEGMENT_HEADER_FMT, raw)
    return seg_id.rstrip(b"\x00").decode("ascii"), allocated, used


def _get_attachment_dir_position(f):
    """Return the file-offset of the AttachmentDirectory segment (0 if absent)."""
    f.seek(0)
    hdr = _read_segment_header(f)
    if hdr is None or hdr[0] != "ZISRAWFILE":
        raise ValueError("Not a valid CZI file (missing ZISRAWFILE header)")
    header_data = f.read(FILE_HEADER_DATA_SIZE)
    return struct.unpack("<q", header_data[ATT_DIR_POSITION_OFFSET:
                                           ATT_DIR_POSITION_OFFSET + 8])[0]


def _parse_attachment_entries(f, att_dir_pos):
    """
    Parse the ZISRAWATTDIR segment.
    Returns (entry_count, list_of_128-byte_entry_blobs, list_of_names).
    """
    f.seek(att_dir_pos)
    seg = _read_segment_header(f)
    if seg is None or seg[0] != "ZISRAWATTDIR":
        raise ValueError(f"Expected ZISRAWATTDIR at offset {att_dir_pos}, got {seg}")

    entry_count = struct.unpack("<i", f.read(4))[0]
    f.read(252)  # reserved

    entries, names = [], []
    for _ in range(entry_count):
        blob = f.read(ATTACHMENT_ENTRY_SIZE)
        name = blob[ATTACHMENT_NAME_OFFSET:
                     ATTACHMENT_NAME_OFFSET + ATTACHMENT_NAME_LENGTH]
        name = name.rstrip(b"\x00").decode("ascii", errors="ignore")
        entries.append(blob)
        names.append(name)
    return entry_count, entries, names


def remove_label(src_path, dst_path=None):
    """
    Remove all 'Label' attachments from a CZI file.

    Parameters
    ----------
    src_path : str  – input .czi file
    dst_path : str  – output path (default: <stem>_LabelRemoved.czi)

    Returns True if a label was found and removed, False otherwise.
    """
    if dst_path is None:
        stem, ext = os.path.splitext(src_path)
        dst_path = f"{stem}_LabelRemoved{ext}"

    with open(src_path, "rb") as f:
        att_dir_pos = _get_attachment_dir_position(f)
        if att_dir_pos == 0:
            print(f"  [skip] No attachment directory: {os.path.basename(src_path)}")
            return False

        orig_count, entries, names = _parse_attachment_entries(f, att_dir_pos)

    # Filter out Label entries
    keep = [(e, n) for e, n in zip(entries, names) if n != "Label"]
    removed = orig_count - len(keep)

    if removed == 0:
        print(f"  [skip] No Label attachment found: {os.path.basename(src_path)}")
        return False

    # Copy file, then patch the attachment directory in-place
    shutil.copy2(src_path, dst_path)

    with open(dst_path, "r+b") as f:
        f.seek(att_dir_pos + SEGMENT_HEADER_SIZE)

        # Rewrite entry count
        f.write(struct.pack("<i", len(keep)))
        f.write(b"\x00" * 252)  # reserved

        # Write retained entries, then zero out the vacated slots
        for entry, _ in keep:
            f.write(entry)
        f.write(b"\x00" * (removed * ATTACHMENT_ENTRY_SIZE))

    print(f"  [done] Removed {removed} Label attachment(s) -> {os.path.basename(dst_path)}")
    return True


def main():
    # Get folder from CLI arg or interactive prompt
    if len(sys.argv) > 1:
        folder = sys.argv[1]
    else:
        folder = input("Enter folder path containing CZI files: ").strip()

    if not os.path.isdir(folder):
        print(f"Error: '{folder}' is not a valid directory.")
        sys.exit(1)

    czi_files = sorted(glob.glob(os.path.join(folder, "*.czi")))
    if not czi_files:
        print(f"No .czi files found in {folder}")
        sys.exit(0)

    print(f"Found {len(czi_files)} CZI file(s) in {folder}\n")

    processed, skipped = 0, 0
    for path in czi_files:
        # Skip files that are already label-removed outputs
        if "_LabelRemoved" in os.path.basename(path):
            continue
        if remove_label(path):
            processed += 1
        else:
            skipped += 1

    print(f"\nDone. {processed} processed, {skipped} skipped.")


if __name__ == "__main__":
    main()