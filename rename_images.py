"""Batch-rename images using an AI-generated description plus local metadata.

Each image is sent to the Inferway chat-completions API (OpenAI-compatible) for a
short description; EXIF date/camera and file size can be added to the new name.
Uses only the standard library (python-dotenv is optional, for loading .env).
"""
import os
import sys
import re
import json
import time
import base64
import struct
import mimetypes
import argparse
import logging
from collections import Counter
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List, Set

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import urllib.request
import urllib.error

BASE_URL = "https://api.inferway.ai/v1"
MODEL_ID = "deepseek/deepseek-v4.1-flash-1017"
REQUEST_TIMEOUT = 60  # seconds; stops one stalled request from hanging the whole batch
LOG_FILE = Path(__file__).with_name("rename_images.log")  # appended to on every CLI run
log = logging.getLogger("image_renamer")
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".gif"}


def get_api_key() -> str:
    """Return INFERWAY_API_KEY from the environment, or exit with an error."""
    key = os.environ.get("INFERWAY_API_KEY")
    if not key:
        print("ERROR: INFERWAY_API_KEY environment variable is not set.", file=sys.stderr)
        print("Set INFERWAY_API_KEY in your shell or add it to .env.", file=sys.stderr)
        sys.exit(1)
    return key


def fetch_model_info(api_key: str, model_id: str) -> Dict[str, Any]:
    """Return the /models entry for model_id (3 attempts); exit if it is missing or unreachable."""
    url = f"{BASE_URL}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = data.get("data", [])
                for model in models:
                    if model.get("id") == model_id:
                        return model
                log.error("Model %r not found in /models", model_id)
                print(f"ERROR: Model '{model_id}' not found.", file=sys.stderr)
                print("Available models:", file=sys.stderr)
                for model in models:
                    print(f" - {model.get('id')}", file=sys.stderr)
                sys.exit(1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            error_code = "UNKNOWN"
            try:
                body = json.loads(exc.read().decode("utf-8"))
                if isinstance(body, dict):
                    err_obj = body.get("error", {})
                    if isinstance(err_obj, dict):
                        error_code = err_obj.get("code") or err_obj.get("message") or "UNKNOWN"
                    else:
                        error_code = str(err_obj)
            except Exception:
                pass
            log.warning("Models fetch attempt %d failed: HTTP %s, %s", attempt + 1, status, error_code)
            print(f"[Attempt {attempt + 1}] Models fetch failed: HTTP {status}, Error Code: {error_code}", file=sys.stderr)
            if attempt == 2:
                sys.exit(1)
            time.sleep(1)
        except Exception as exc:
            log.warning("Models fetch attempt %d error: %s", attempt + 1, exc)
            print(f"[Attempt {attempt + 1}] Models fetch error: {exc}", file=sys.stderr)
            if attempt == 2:
                sys.exit(1)
            time.sleep(1)

    sys.exit(1)


def parse_tiff(data: bytes, metadata: Dict[str, Any]) -> None:
    """Fill metadata with make, camera (model) and date_taken from a TIFF/EXIF block.

    Minimal parser: reads IFD0 and the Exif sub-IFD only. DateTimeOriginal (0x9003)
    wins over the IFD0 DateTime (0x0132).
    """
    if len(data) < 8:
        return
    # "II" = little-endian (Intel), "MM" = big-endian (Motorola).
    endian = "<" if data[:2] == b"II" else ">"
    try:
        tag_magic, first_ifd_offset = struct.unpack(f"{endian}HI", data[2:8])
        if tag_magic != 42:
            return
    except struct.error:
        return

    def read_ifd(offset: int):
        # Each 12-byte entry: tag, type, count, then the value itself (if <= 4 bytes) or an offset to it.
        if offset + 2 > len(data):
            return
        try:
            num_entries = struct.unpack(f"{endian}H", data[offset:offset + 2])[0]
        except struct.error:
            return
        cur = offset + 2
        for _ in range(num_entries):
            if cur + 12 > len(data):
                break
            try:
                tag, tag_type, count, val_or_offset = struct.unpack(f"{endian}HHI4s", data[cur:cur + 12])
            except struct.error:
                break
            cur += 12
            val = None
            if tag_type == 2:  # ASCII
                val_len = count
                if val_len <= 4:
                    val_bytes = val_or_offset[:val_len]
                else:
                    try:
                        off = struct.unpack(f"{endian}I", val_or_offset)[0]
                        val_bytes = data[off:off + val_len] if off + val_len <= len(data) else b""
                    except struct.error:
                        val_bytes = b""
                val = val_bytes.decode("ascii", errors="ignore").rstrip("\x00").strip()
            elif tag_type in (3, 4):  # SHORT or LONG
                if count == 1:
                    fmt = f"{endian}H" if tag_type == 3 else f"{endian}I"
                    try:
                        val = struct.unpack(fmt, val_or_offset[:2 if tag_type == 3 else 4])[0]
                    except struct.error:
                        val = None

            if tag == 0x010F and "make" not in metadata:
                metadata["make"] = val
            elif tag == 0x0110 and "camera" not in metadata:
                metadata["camera"] = val
            elif tag == 0x0132 and "date_taken" not in metadata:
                metadata["date_taken"] = val
            elif tag == 0x9003:  # DateTimeOriginal
                metadata["date_taken"] = val
            elif tag == 0x8769 and tag_type in (3, 4):  # Exif SubIFD
                try:
                    exif_off = struct.unpack(f"{endian}I", val_or_offset)[0]
                    read_ifd(exif_off)
                except struct.error:
                    pass

    read_ifd(first_ifd_offset)


def extract_exif_metadata(file_path: Path) -> Dict[str, Any]:
    """Read EXIF from a JPEG or TIFF file; returns {} for other formats or unreadable files."""
    metadata: Dict[str, Any] = {}
    try:
        with open(file_path, "rb") as f:
            header = f.read(2)
            if header == b"\xff\xd8":  # JPEG
                while True:
                    marker_bytes = f.read(2)
                    if len(marker_bytes) < 2:
                        break
                    marker, = struct.unpack(">2s", marker_bytes)
                    if marker in (b"\xff\xd9", b"\xff\xda"):
                        break
                    length_bytes = f.read(2)
                    if len(length_bytes) < 2:
                        break
                    length, = struct.unpack(">H", length_bytes)
                    if marker == b"\xff\xe1":
                        data = f.read(length - 2)
                        # APP1 can also hold XMP; keep scanning until the Exif one.
                        if data.startswith(b"Exif\x00\x00"):
                            parse_tiff(data[6:], metadata)
                            break
                    else:
                        f.seek(length - 2, os.SEEK_CUR)
            elif header in (b"II", b"MM"):  # TIFF
                f.seek(0)
                data = f.read(65536)  # shortcut: tags past the first 64KB are missed, read more if real TIFFs lose metadata
                parse_tiff(data, metadata)
    except Exception:
        pass
    return metadata


def format_file_size(size_bytes: int) -> str:
    """Human-readable size for filenames, e.g. 512B, 250KB, 2.6MB."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes // 1024}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def get_image_metadata(file_path: Path, date_format: str = "%Y-%m-%d_%H-%M-%S") -> Dict[str, str]:
    """Return the date, camera, size and orig values used in filenames.

    The date comes from EXIF, falling back to the file modification time.
    """
    exif = extract_exif_metadata(file_path)
    stat = file_path.stat()

    date_str = ""
    raw_date = exif.get("date_taken")
    if raw_date:
        try:
            dt = datetime.strptime(raw_date, "%Y:%m:%d %H:%M:%S")
            date_str = dt.strftime(date_format)
        except ValueError:
            pass

    if not date_str:
        dt = datetime.fromtimestamp(stat.st_mtime)
        date_str = dt.strftime(date_format)

    camera = exif.get("camera") or exif.get("make") or ""
    if camera:
        camera = re.sub(r"[^\w\-_]+", "_", camera).strip("_")

    size_str = format_file_size(stat.st_size)

    return {
        "date": date_str,
        "camera": camera,
        "size": size_str,
        "orig": file_path.stem
    }


def encode_image(file_path: Path) -> str:
    """Return the image as a base64 data URL; GIF/BMP are converted to PNG via Tk when possible."""
    suffix = file_path.suffix.lower()
    if suffix in {".gif", ".bmp"}:
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            try:
                img = tkinter.PhotoImage(file=str(file_path))
                temp_png = file_path.with_name(f"_tmp_{file_path.stem}.png")
                try:
                    img.write(str(temp_png), format="png")
                    with open(temp_png, "rb") as f:
                        encoded = base64.b64encode(f.read()).decode("utf-8")
                finally:
                    temp_png.unlink(missing_ok=True)
                return f"data:image/png;base64,{encoded}"
            finally:
                root.destroy()
        except Exception:
            pass  # No Tk, or Tk cannot read this file: send the original bytes instead.

    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "image/png"
    with open(file_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def generate_description(api_key: str, model_id: str, context_length: Optional[int], image_data_url: str, max_words: int = 14) -> Optional[str]:
    """Ask the vision model for a short image description; None after 3 failed attempts."""
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    prompt = (
        f"Analyze this image and describe its subject clearly in less than {max_words + 1} words for a filename. "
        "Return ONLY the words separated by single spaces or underscores. "
        "Do not include quotes, file extensions, or punctuation."
    )

    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}}
                ]
            }
        ],
        "temperature": 0.2
    }

    if context_length:
        payload["max_tokens"] = min(60, context_length)

    req_data = json.dumps(payload).encode("utf-8")

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                choices = resp_json.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "").strip()
                    return content
                return None
        except urllib.error.HTTPError as exc:
            status = exc.code
            error_code = "UNKNOWN"
            error_msg = ""
            try:
                body = json.loads(exc.read().decode("utf-8"))
                if isinstance(body, dict):
                    err_obj = body.get("error", {})
                    if isinstance(err_obj, dict):
                        error_code = err_obj.get("code") or err_obj.get("type") or "UNKNOWN"
                        error_msg = err_obj.get("message", "")
                    else:
                        error_code = str(err_obj)
            except Exception:
                pass
            msg_suffix = f" ({error_msg})" if error_msg else ""
            log.warning("Description attempt %d failed: HTTP %s, %s%s", attempt + 1, status, error_code, msg_suffix)
            print(f"[Attempt {attempt + 1}] Description request failed: HTTP {status}, Error Code: {error_code}{msg_suffix}", file=sys.stderr)
            if attempt == 2:
                return None
            time.sleep(1)
        except Exception as exc:
            log.warning("Description attempt %d error: %s", attempt + 1, exc)
            print(f"[Attempt {attempt + 1}] Description request error: {exc}", file=sys.stderr)
            if attempt == 2:
                return None
            time.sleep(1)

    return None


def sanitize_filename(description: str, max_words: int = 14) -> str:
    """Turn model output into a safe lowercase_snake name of at most max_words words."""
    # Dots are stripped too, so the model cannot inject a fake extension.
    cleaned = re.sub(r"[\r\n\t]+", " ", description)
    cleaned = re.sub(r'[\\/:*?"<>|.]+', "", cleaned).strip()
    words = cleaned.split()
    if len(words) > max_words:
        words = words[:max_words]
    name = "_".join(words).lower()
    return name if name else "unnamed_image"


def build_new_filename(
    metadata: Dict[str, str],
    description: str,
    pattern: Optional[str] = None,
    include_date: bool = False,
    include_camera: bool = False,
    include_size: bool = False,
    ext: str = ".jpg"
) -> str:
    """Build the new filename from --pattern if given, otherwise from the --include-* flags."""
    if pattern:
        rendered = pattern.format(
            desc=description,
            date=metadata.get("date", ""),
            camera=metadata.get("camera", ""),
            size=metadata.get("size", ""),
            orig=metadata.get("orig", "")
        )
        rendered = re.sub(r"_+", "_", rendered).strip("_")
        base_name = rendered if rendered else description
    else:
        parts: List[str] = []
        if include_date and metadata.get("date"):
            parts.append(metadata["date"])
        if include_camera and metadata.get("camera"):
            parts.append(metadata["camera"])
        parts.append(description)
        if include_size and metadata.get("size"):
            parts.append(metadata["size"])
        base_name = "_".join(p for p in parts if p)

    return f"{base_name}{ext}"


def is_already_renamed(file_path: Path, include_date: bool, pattern: Optional[str]) -> bool:
    """Guess whether a file was renamed before; only detectable when new names start with a date."""
    stem = file_path.stem
    if include_date or (pattern and "{date}" in pattern):
        if re.match(r"^\d{4}-\d{2}-\d{2}", stem):
            return True
    return False


def resolve_target_path(
    folder_path: Path,
    target_filename: str,
    current_path: Path,
    claimed_paths: Set[Path]
) -> Path:
    """Return a free path for target_filename, adding _1, _2, ... on collisions.

    claimed_paths holds targets already picked in this run, so two images with the same
    description never share a name (this also covers dry runs, where nothing is on disk yet).
    """
    target_path = folder_path / target_filename
    if target_path == current_path and target_path not in claimed_paths:
        claimed_paths.add(target_path)
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    counter = 1

    while target_path in claimed_paths or (target_path.exists() and target_path != current_path):
        target_path = folder_path / f"{stem}_{counter}{suffix}"
        counter += 1

    claimed_paths.add(target_path)
    return target_path


def rename_images_in_folder(
    folder_path: Path,
    dry_run: bool = False,
    pattern: Optional[str] = None,
    include_date: bool = False,
    include_camera: bool = False,
    include_size: bool = False,
    date_format: str = "%Y-%m-%d_%H-%M-%S",
    preserve_extension: bool = False,
    skip_renamed: bool = False,
    max_words: int = 14,
    recursive: bool = False,
    api_key: Optional[str] = None,
    context_length: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Describe and rename every supported image in folder_path.

    Returns one dict per image with original, target and status
    (renamed, dry_run, unchanged, skipped, failed or error).
    Passing api_key and context_length skips the env lookup and /models call (used by tests).
    """
    if not api_key:
        api_key = get_api_key()
    if context_length is None:
        print(f"Fetching model details for '{MODEL_ID}'...")
        model_info = fetch_model_info(api_key, MODEL_ID)
        context_length = model_info.get("context_length")
        print(f"Model: {MODEL_ID}, context_length: {context_length}")
    log.info("Model: %s, context_length: %s", MODEL_ID, context_length)

    if recursive:
        image_files = [
            f for f in folder_path.rglob("*")
            if f.is_file() and f.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ]
    else:
        image_files = [
            f for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ]

    if not image_files:
        print("No image files found.")
        log.info("No image files found in %s", folder_path)
        return []

    print(f"Found {len(image_files)} image file(s) to process.")
    log.info("Found %d image file(s) in %s", len(image_files), folder_path)
    results = []
    claimed_paths: Set[Path] = set()

    def record(original: Path, status: str, target: Optional[Path], error: str = "") -> None:
        # Logged per file as it happens, so a crashed run still leaves a full trail for manual undo.
        entry: Dict[str, Any] = {"original": original, "target": target, "status": status}
        if error:
            entry["error"] = error
        results.append(entry)
        level = logging.ERROR if status in ("failed", "error") else logging.INFO
        log.log(level, "%s: %s -> %s%s", status, original, target, f" ({error})" if error else "")

    for img_path in image_files:
        print(f"\nProcessing: {img_path.name}")

        if skip_renamed and is_already_renamed(img_path, include_date, pattern):
            print(f"Skipping '{img_path.name}': file matches already renamed format.")
            record(img_path, "skipped", img_path)
            continue

        metadata = get_image_metadata(img_path, date_format=date_format)
        image_data_url = encode_image(img_path)
        raw_desc = generate_description(api_key, MODEL_ID, context_length, image_data_url, max_words=max_words)
        if not raw_desc:
            print(f"Skipping '{img_path.name}': failed to obtain description.")
            record(img_path, "failed", None, "no description from API")
            continue

        desc = sanitize_filename(raw_desc, max_words=max_words)
        ext = img_path.suffix if preserve_extension else img_path.suffix.lower()

        new_filename = build_new_filename(
            metadata=metadata,
            description=desc,
            pattern=pattern,
            include_date=include_date,
            include_camera=include_camera,
            include_size=include_size,
            ext=ext
        )

        target_path = resolve_target_path(img_path.parent, new_filename, img_path, claimed_paths)

        if target_path == img_path:
            print(f"File '{img_path.name}' already has matching name.")
            record(img_path, "unchanged", target_path)
            continue

        if dry_run:
            print(f"[DRY-RUN] Would rename: '{img_path.name}' -> '{target_path.name}'")
            record(img_path, "dry_run", target_path)
        else:
            try:
                img_path.rename(target_path)
                print(f"Renamed: '{img_path.name}' -> '{target_path.name}'")
                record(img_path, "renamed", target_path)
            except OSError as e:
                print(f"Error renaming '{img_path.name}': {e}", file=sys.stderr)
                record(img_path, "error", target_path, str(e))

    counts = Counter(r["status"] for r in results)
    log.info("Summary: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return results


def parse_arguments() -> argparse.Namespace:
    """Parse command-line options (the README has the full table)."""
    parser = argparse.ArgumentParser(
        description="Batch rename images based on AI content description and metadata."
    )
    parser.add_argument(
        "--folder", "--dir",
        dest="folder",
        type=str,
        default=".",
        help="Path to folder containing images (default: current directory)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview planned changes without modifying any files."
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default=None,
        help="Custom naming pattern with placeholders {desc}, {date}, {camera}, {size}, {orig}."
    )
    parser.add_argument(
        "--include-date",
        action="store_true",
        help="Prepend date taken (or modification date) to the file name."
    )
    parser.add_argument(
        "--include-camera",
        action="store_true",
        help="Include camera make or model in the file name."
    )
    parser.add_argument(
        "--include-size",
        action="store_true",
        help="Append file size to the file name."
    )
    parser.add_argument(
        "--date-format",
        type=str,
        default="%Y-%m-%d_%H-%M-%S",
        help="Format for dates (default: '%%Y-%%m-%%d_%%H-%%M-%%S')."
    )
    parser.add_argument(
        "--preserve-extension",
        action="store_true",
        help="Preserve original extension case (default converts to lowercase)."
    )
    parser.add_argument(
        "--skip-renamed",
        action="store_true",
        help="Skip files that appear to have been already renamed."
    )
    parser.add_argument(
        "--max-words",
        type=int,
        default=14,
        help="Maximum words for AI description (default: 14)."
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Process subdirectories recursively."
    )
    args = parser.parse_args()
    if args.pattern:
        # Catch placeholder typos now, before any API calls are spent.
        try:
            args.pattern.format(desc="", date="", camera="", size="", orig="")
        except (KeyError, IndexError, ValueError) as exc:
            parser.error(f"invalid --pattern {args.pattern!r} ({exc!r}); allowed: {{desc}} {{date}} {{camera}} {{size}} {{orig}}")
    return args


if __name__ == "__main__":
    args = parse_arguments()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    log.info("===== Run started: %s", vars(args))
    target_folder = Path(args.folder).resolve()
    if not target_folder.exists() or not target_folder.is_dir():
        log.error("Folder %s does not exist or is not a directory", target_folder)
        print(f"ERROR: Folder '{target_folder}' does not exist or is not a directory.", file=sys.stderr)
        sys.exit(1)

    print(f"Logging to {LOG_FILE}")
    try:
        rename_images_in_folder(
            folder_path=target_folder,
            dry_run=args.dry_run,
            pattern=args.pattern,
            include_date=args.include_date,
            include_camera=args.include_camera,
            include_size=args.include_size,
            date_format=args.date_format,
            preserve_extension=args.preserve_extension,
            skip_renamed=args.skip_renamed,
            max_words=args.max_words,
            recursive=args.recursive
        )
    except BaseException:
        # BaseException also catches sys.exit from API failures and Ctrl+C, so aborted runs are marked too.
        log.exception("===== Run aborted")
        raise
    log.info("===== Run finished")
