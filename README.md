# Image Renamer AI

Renames image files after what they show. Each image goes to a vision model through the Inferway API, which returns a short description such as `blue_mountain_lake`. You can also add the EXIF date, the camera, or the file size to the name.

```text
IMG_0042.JPG  ->  2024-08-15_14-30-00_blue_mountain_lake.jpg
```

## Features

- AI descriptions, cleaned into safe `lowercase_snake_case` filenames.
- EXIF date and camera from JPEG and TIFF, read with a built-in parser (no Pillow needed). The date falls back to the file's modified time.
- Custom naming patterns with placeholders.
- A dry-run mode that previews changes and never writes.
- Collision-safe: existing files are never overwritten, and a `_1`, `_2`, ... suffix is added instead.
- Standard library only. `python-dotenv` is optional and only loads `.env`.

Supported formats: `.jpg .jpeg .png .tiff .tif .bmp .webp .gif`. GIF and BMP are converted to PNG with Tk (tkinter) before upload when Tk can read them. Otherwise the original bytes are sent.

## Prerequisites

- Python 3.8 or later.
- An Inferway API key.
- Optional: `pip install -r requirements.txt` to load the key from `.env`.

## Setup

1. Optional: Install dependencies to load `.env` variables:
   ```powershell
   pip install -r requirements.txt
   ```

2. Copy `.env.sample` to `.env` and put your key in it:
   ```env
   INFERWAY_API_KEY=your_api_key_here
   ```
   Or set it in your shell instead:
   - Linux/macOS: `export INFERWAY_API_KEY="your_api_key_here"`
   - Windows PowerShell: `$env:INFERWAY_API_KEY="your_api_key_here"`

   Without `python-dotenv`, `.env` is ignored and you must set the variable in your shell.

3. Check the key and model with the smoke test:
   ```powershell
   python smoke_test.py
   ```
   It lists the models, checks that the configured model exists, and streams a one-line reply.

## Usage

Always preview first:

```powershell
python rename_images.py --folder "C:\Photos\Trip" --dry-run --include-date
```

Then run the same command without `--dry-run` to apply the renames.

## Command-Line Arguments

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--folder`, `--dir` | Path | `.` | Folder that contains the images. |
| `--dry-run` | Flag | off | Show the planned renames without changing any files. |
| `--pattern` | String | none | Custom filename template (see below). Overrides the `--include-*` flags. |
| `--include-date` | Flag | off | Put the image date at the start of the name. |
| `--include-camera` | Flag | off | Add the camera model (or make) to the name. |
| `--include-size` | Flag | off | Add the file size to the end of the name. |
| `--date-format` | String | `%Y-%m-%d_%H-%M-%S` | `strftime` format for dates. |
| `--preserve-extension` | Flag | off | Keep the extension's original case (the default lowercases it). |
| `--skip-renamed` | Flag | off | Skip files whose name already starts with a `YYYY-MM-DD` date. Only applies with `--include-date` or a `{date}` pattern. |
| `--max-words` | Integer | `14` | Maximum number of words in the description. |
| `--recursive` | Flag | off | Also process images in subfolders. |

## Pattern Syntax

| Placeholder | Value |
| :--- | :--- |
| `{desc}` | AI description, e.g. `blue_mountain_lake` |
| `{date}` | EXIF date taken, or the file modified time, in `--date-format` |
| `{camera}` | EXIF camera model or make, e.g. `Nikon_D850` |
| `{size}` | File size, e.g. `250KB`, `2.6MB` |
| `{orig}` | Original filename without the extension |

Empty placeholders are dropped, and repeated underscores are collapsed. An unknown placeholder (for example `{dsc}`) is rejected before any API call is made.

## Examples

| Command | Example result |
| :--- | :--- |
| `--pattern "{date}_{desc}"` | `2024-08-15_14-30-00_blue_mountain_lake.jpg` |
| `--pattern "{camera}_{desc}_{size}"` | `Nikon_D850_blue_mountain_lake_250KB.jpg` |
| `--pattern "{date}_{camera}_{desc}_{size}"` | `2024-08-15_14-30-00_Nikon_D850_blue_mountain_lake_250KB.jpg` |
| `--pattern "{orig}_{desc}"` | `IMG_0042_red_sports_car.jpg` |
| `--include-date --include-size` | `2024-08-15_14-30-00_smiling_woman_in_park_1.2MB.jpg` |

## Configuration

The model and endpoint are constants at the top of `rename_images.py`:

- `BASE_URL`: `https://api.inferway.ai/v1`
- `MODEL_ID`: `deepseek/deepseek-v4.1-flash-1017`. It must be a vision-capable model.
- `REQUEST_TIMEOUT`: 60 seconds per API request.

Each failed request is retried up to 3 times. If an image still has no description after that, it is skipped and left unchanged.

## Run Log

Every run appends to `rename_images.log`, which sits next to the script and is git-ignored. Each entry has a timestamp and records:

- When the run started, with all the options you used, and the model it used.
- One line per file with its status and the full old and new paths, written as soon as that file is handled.
- API retry warnings and rename errors.
- A summary count per status, then a `Run finished` or `Run aborted` line. Aborted runs include the traceback.

```text
2026-11-18 10:02:11,204 INFO ===== Run started: {'folder': 'C:/Photos', 'dry_run': False, ...}
2026-11-18 10:02:11,830 INFO Model: deepseek/deepseek-v4.1-flash-1017, context_length: 1048576
2026-11-18 10:02:14,517 INFO renamed: C:\Photos\IMG_0042.JPG -> C:\Photos\blue_mountain_lake.jpg
2026-11-18 10:02:17,093 ERROR failed: C:\Photos\IMG_0043.JPG -> None (no description from API)
2026-11-18 10:02:17,094 INFO Summary: failed=1, renamed=1
2026-11-18 10:02:17,094 INFO ===== Run finished
```

Statuses: `renamed`, `dry_run`, `unchanged`, `skipped`, `failed` (no description), `error` (the rename itself failed).

## Safety Notes

- Run `--dry-run` first. There is no automatic undo, but the `renamed:` lines in `rename_images.log` show the original name of every file.
- Image contents are uploaded to the Inferway API.
- Don't put `/`, `\` or `:` in `--date-format`. Those characters aren't valid in filenames.

## Running Tests

The tests don't use the network (the API call is mocked):

```powershell
python -m unittest test_rename_images.py
```

## Project Files

| File | Purpose |
| :--- | :--- |
| `rename_images.py` | The CLI: metadata extraction, API calls, naming, renaming. |
| `smoke_test.py` | Checks the API key and model connection. |
| `test_rename_images.py` | Unit tests. They build mock JPEGs with EXIF data. |
| `requirements.txt` | Dependency list (`python-dotenv` for `.env` support). |
| `.env.sample` | Template for `.env`. |
| `rename_images.log` | Run log, created on the first run (git-ignored). |
