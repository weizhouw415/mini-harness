"""Bounded previews for files already approved for a run.

The caller owns path authorization and storage. Preview images are re-encoded
as ordinary raster images; the original file is never modified.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image, ImageOps

MAX_IMAGE_BYTES = 768 * 1024
MAX_IMAGES = 8
MAX_TEXT_CHARS = 8_000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_RASTER_PIXELS = 40_000_000
_MAX_XML_BYTES = 2 * 1024 * 1024
_CONVERTER_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME",
    "FONTCONFIG_PATH", "FONTCONFIG_FILE", "SYSTEMROOT", "WINDIR", "USERPROFILE",
)
_CONVERTER_LIMITS = """
import os, resource, sys

def cap(kind, limit):
    soft, _ = resource.getrlimit(kind)
    target = min(soft, limit) if soft != resource.RLIM_INFINITY else limit
    resource.setrlimit(kind, (target, target))

cap(resource.RLIMIT_FSIZE, 32 * 1024 * 1024)
cap(resource.RLIMIT_CPU, int(sys.argv[1]))
cap(resource.RLIMIT_CORE, 0)
if sys.platform.startswith('linux'):
    cap(resource.RLIMIT_AS, 768 * 1024 * 1024)
os.execv(sys.argv[2], sys.argv[2:])
"""
_RASTER = {".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jpeg", ".jpg",
           ".png", ".tif", ".tiff", ".webp"}
_TEXT = {".jsonl", ".log", ".md", ".ndjson", ".rst", ".sql", ".toml", ".txt", ".yaml", ".yml"}
_VIDEO = {".avi": "avi", ".m4v": "mov", ".mkv": "matroska", ".mov": "mov", ".mp4": "mov",
          ".mpeg": "mpeg", ".mpg": "mpeg", ".webm": "matroska"}
_AUDIO = {".aac": "aac", ".aiff": "aiff", ".flac": "flac", ".m4a": "mov", ".mp3": "mp3",
          ".oga": "ogg", ".ogg": "ogg", ".opus": "ogg", ".wav": "wav"}


def _text(text: str) -> dict[str, str]:
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[Preview truncated.]"
    return {"type": "text", "text": text}


def _image(image: Image.Image, name: str, edge: int, *, variant: str = "") -> dict[str, str]:
    mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
    image = image.convert(mode)
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    # Pillow may carry source ICC, EXIF or textual metadata through conversions.
    image.info.clear()
    output = io.BytesIO()
    image.save(output, format="PNG")
    suffix, mime_type = "png", "image/png"
    if output.tell() > MAX_IMAGE_BYTES:
        if mode == "RGBA":
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background
        suffix, mime_type = "jpg", "image/jpeg"
        while True:
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            if output.tell() <= MAX_IMAGE_BYTES:
                break
            image.thumbnail(tuple(max(1, dimension * 3 // 4) for dimension in image.size),
                            Image.Resampling.LANCZOS)
    ending = f"{variant}.{suffix}"
    stem = name[:160 - len(ending)].encode("utf-8", errors="replace")
    stem = stem[:240 - len(ending)].decode("utf-8", errors="ignore") or "preview"
    return {"type": "image", "name": f"{stem}{ending}", "mime_type": mime_type,
            "data_base64": base64.b64encode(output.getbuffer()).decode("ascii")}


def _raster(path: Path, name: Path, edge: int, crop: tuple[int, int, int, int] | None,
            limit: int, frames: int | None, automatic: bool) -> list[dict[str, str]]:
    with Image.open(path) as source:
        if source.width * source.height > _MAX_RASTER_PIXELS:
            raise ValueError("Image dimensions exceed the preview limit.")
        frame_count = 1
        if source.format in {"GIF", "WEBP", "PNG"} and getattr(source, "is_animated", False):
            # Seek at most 120 frames to keep degenerate animations bounded.
            for frame_count in range(1, 120):
                try:
                    source.seek(frame_count)
                except EOFError:
                    break
            else:
                frame_count = 120
        take = min(limit, frames or (3 if automatic else 6), frame_count)
        indices = range(take) if take <= 1 else (
            round(index * (frame_count - 1) / (take - 1)) for index in range(take)
        )
        label = f"{name.name} · {source.width} × {source.height} · {source.format}"
        if frame_count > 1:
            label += f" · {'at least ' if frame_count == 120 else ''}{frame_count} frames"
        blocks = [_text(label)]
        for index in indices:
            source.seek(index)
            image = ImageOps.exif_transpose(source)
            if crop:
                x, y, width, height = crop
                if x + width > image.width or y + height > image.height:
                    raise ValueError("Crop extends beyond the image dimensions.")
                image = image.crop((x, y, x + width, y + height))
            variant = f"-frame-{index + 1}" if frame_count > 1 else ""
            blocks.append(_image(image, name.stem, edge, variant=variant))
        return blocks


def _read_text(path: Path, limit: int = MAX_TEXT_CHARS * 4) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    value = data[:limit].decode(encoding, errors="replace")
    return value + "\n[Preview truncated.]" if len(data) > limit else value


def _table(path: Path, name: Path) -> list[dict[str, str]]:
    sample = _read_text(path)
    reader = csv.reader(io.StringIO(sample), delimiter="\t" if name.suffix.lower() == ".tsv" else ",")
    rows = []
    for _ in range(12):
        row = next(reader, None)
        if row is None:
            break
        rows.append([value[:180] for value in row[:16]])
    return [_text(f"{name.name} · First {len(rows)} rows, up to 16 columns\n"
                  + json.dumps(rows, ensure_ascii=False, indent=2))]


class _NoDoctype(ET.TreeBuilder):
    def doctype(self, name: str, public_id: str | None, system_id: str | None) -> None:
        raise ValueError("XML document type declarations are unavailable in previews.")


def _xml(data: bytes) -> ET.Element:
    return ET.fromstring(data, parser=ET.XMLParser(target=_NoDoctype()))


def _archive_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    with archive.open(name) as member:
        data = member.read(_MAX_XML_BYTES + 1)
    if len(data) > _MAX_XML_BYTES:
        raise ValueError("An Office document part exceeds the preview limit.")
    return _xml(data)


def _office_text(root: ET.Element) -> str:
    return "\n".join(text for node in root.iter() if node.tag.endswith("}p")
                     and (text := "".join(child.text or "" for child in node.iter()
                                          if child.tag.endswith("}t"))))[:MAX_TEXT_CHARS]


def _pages(value: str | None, default: list[int], limit: int) -> list[int]:
    if not value:
        return default[:limit]
    pages: list[int] = []
    if len(value) > 200:
        raise ValueError("Page selection is too long.")
    for part in value.split(","):
        match = re.fullmatch(r"\s*([1-9]\d{0,4})(?:-([1-9]\d{0,4}))?\s*", part)
        if not match:
            raise ValueError("Select one-based pages, for example 1-3,5.")
        start, end = int(match[1]), int(match[2] or match[1])
        if end < start:
            raise ValueError("Page range ends before it starts.")
        for page in range(start, end + 1):
            if page not in pages:
                pages.append(page)
            if len(pages) > limit:
                raise ValueError(f"Select up to {limit} pages per preview.")
    return pages


def _spreadsheet(archive: zipfile.ZipFile, sheet: str | None) -> str:
    workbook = _archive_xml(archive, "xl/workbook.xml")
    relationships = _archive_xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {item.get("Id"): item.get("Target", "") for item in relationships
               if item.get("TargetMode") != "External"}
    sheets = {item.get("name", ""): item for item in workbook.findall(".//{*}sheet")}
    names = ", ".join(sheets)
    if not sheets:
        return "No worksheets found."
    name = sheet if sheet is not None else next(iter(sheets))
    if name not in sheets:
        return f"Worksheet {name!r} was not found. Worksheets: {names}"
    relation = next((value for key, value in sheets[name].attrib.items() if key.endswith("}id")), "")
    target = targets.get(relation, "")
    target = posixpath.normpath(posixpath.join("xl", target)) if not target.startswith("/") else target[1:]
    if not target.startswith("xl/"):
        raise ValueError("The worksheet points outside the workbook.")
    table = _archive_xml(archive, target)
    shared_strings = []
    if "xl/sharedStrings.xml" in archive.namelist():
        strings = _archive_xml(archive, "xl/sharedStrings.xml")
        shared_strings = ["".join(node.text or "" for node in item.findall(".//{*}t")) for item in strings]
    rows = []
    for row in table.findall(".//{*}row")[:12]:
        cells = {}
        for cell in row.findall("{*}c")[:16]:
            value = cell.findtext("{*}v", default="")
            if cell.get("t") == "s" and value.isascii() and value.isdigit() and len(value) < 8:
                index = int(value)
                value = shared_strings[index] if index < len(shared_strings) else value
            elif cell.get("t") == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(".//{*}t"))
            cells[cell.get("r", str(len(cells) + 1))] = value[:180]
        rows.append(cells)
    dimensions = table.find("{*}dimension")
    extent = dimensions.get("ref", "unknown") if dimensions is not None else "unknown"
    return f"Worksheets: {names}\n{name} · {extent} · Sample cells\n" + json.dumps(
        rows, ensure_ascii=False, indent=2,
    )


def _office(path: Path, name: Path, pages: str | None, sheet: str | None) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        suffix = name.suffix.lower()
        if suffix == ".docx":
            summary = _office_text(_archive_xml(archive, "word/document.xml"))
        elif suffix == ".xlsx":
            summary = _spreadsheet(archive, sheet)
        else:
            slide_files = [(int(match[1]), name) for name in archive.namelist()
                           if (match := re.fullmatch(r"ppt/slides/slide(\d{1,6})\.xml", name))]
            slides = dict(sorted(slide_files))
            selected = _pages(pages, list(slides), 8)
            summary = f"{len(slides)} slides\n" + "\n".join(
                f"Slide {slide}: " + _office_text(_archive_xml(archive, slides[slide]))[:900]
                if slide in slides else f"Slide {slide} was not found."
                for slide in selected
            )
    return [_text(f"{name.name}\n{summary or '[No extractable text.]'}")]


def _command(command: list[str], timeout: int = 8, *, input_data: bytes | None = None,
             output: BinaryIO | None = None) -> bool:
    if os.name == "posix":
        # Set resource limits in a fresh process. Python preexec_fn is unsafe in a threaded worker.
        command = [sys.executable, "-I", "-c", _CONVERTER_LIMITS, str(timeout), *command]
    try:
        return subprocess.run(command, input=input_data,
                              stdin=subprocess.DEVNULL if input_data is None else None,
                              stdout=output if output is not None else subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                              env={key: os.environ[key] for key in _CONVERTER_ENV if key in os.environ}).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _svg(path: Path, name: Path, edge: int, limit: int) -> list[dict[str, str]]:
    converter = shutil.which("rsvg-convert") if limit else None
    if converter:
        with path.open("rb") as source:
            data = source.read(_MAX_XML_BYTES + 1)
        if len(data) <= _MAX_XML_BYTES and _xml(data).tag in {"svg", "{http://www.w3.org/2000/svg}svg"}:
            with tempfile.TemporaryDirectory(prefix="harness-svg-") as directory:
                target = Path(directory) / "preview.png"
                # stdin has no base URL in librsvg, preventing external file or network reads.
                if _command([converter, f"--width={edge}", f"--height={edge}", "--keep-aspect-ratio",
                             f"--output={target}"], input_data=data) and target.is_file():
                    with Image.open(target) as rendered:
                        return [_text(f"{name.name} · SVG"), _image(rendered, name.stem, edge)]
    return [_text(f"{name.name} · SVG source\n{_read_text(path)}")]


def _pdf(path: Path, name: Path, edge: int, pages: str | None, limit: int, automatic: bool,
         overview: bool) -> list[dict[str, str]]:
    defaults = list(range(1, 6)) if overview else [1, 2] if automatic else [1]
    selected = _pages(pages, defaults, 5)
    blocks = [_text(f"{name.name} · PDF")]
    converter = shutil.which("pdftoppm") if limit else None
    extractor = shutil.which("pdftotext")
    with tempfile.TemporaryDirectory(prefix="harness-preview-") as directory:
        temp = Path(directory)
        extracted = temp / "text.txt"
        if extractor and _command([extractor, "-f", str(selected[0]), "-l", str(selected[0]),
                                   "-enc", "UTF-8", str(path), str(extracted)]):
            content = _read_text(extracted).strip()
            if content:
                blocks.append(_text(f"Page {selected[0]} text\n{content}"))
        if converter:
            unavailable = []
            prefix = temp / "page"
            output = prefix.with_suffix(".png")
            for page in selected[:limit]:
                output.unlink(missing_ok=True)
                if _command([converter, "-f", str(page), "-l", str(page), "-scale-to", str(edge),
                             "-png", "-singlefile", str(path), str(prefix)]) and output.is_file():
                    with Image.open(output) as rendered:
                        blocks.append(_image(rendered, name.stem, edge, variant=f"-page-{page}"))
                else:
                    unavailable.append(page)
            if unavailable and (pages or not any(block["type"] == "image" for block in blocks)):
                blocks.append(_text(f"PDF pages unavailable: {', '.join(map(str, unavailable))}."))
        elif limit:
            blocks.append(_text("PDF page rendering requires pdftoppm (Poppler)."))
    return blocks


def _office_pages(path: Path, edge: int, pages: str | None, limit: int,
                  overview: bool) -> list[dict[str, str]]:
    converter = shutil.which("libreoffice") or shutil.which("soffice")
    if not converter:
        return [_text("Office page rendering requires LibreOffice. Text and structure are shown when available.")]
    with tempfile.TemporaryDirectory(prefix="harness-office-") as directory:
        temp = Path(directory)
        profile = temp / "profile"
        (profile / "user").mkdir(parents=True)
        (profile / "user" / "registrymodifications.xcu").write_text(
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding="utf-8",
        )
        if _command([converter, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
                     "--convert-to", "pdf", "--outdir", str(temp), str(path)], timeout=20):
            rendered = temp / f"{path.stem}.pdf"
            if rendered.is_file():
                return _pdf(rendered, rendered, edge, pages, limit, False, overview)
    return [_text("LibreOffice could not render this document.")]


def _media(path: Path, name: Path, edge: int, limit: int, frames: int | None, start: float | None,
           end: float | None, fps: float | None, automatic: bool) -> list[dict[str, str]]:
    probe = shutil.which("ffprobe")
    converter = shutil.which("ffmpeg")
    suffix = name.suffix.lower()
    is_video = suffix in _VIDEO
    label = f"{name.name} · {'Video' if is_video else 'Audio'}"
    if not probe:
        return [_text(label + " · Media details require ffprobe.")]
    # Force the expected container and disable network protocols even for misleading extensions.
    source = ["-max_alloc", str(64 * 1024 * 1024), "-protocol_whitelist", "file,pipe", "-f",
              (_VIDEO | _AUDIO)[suffix], "-i", str(path)]
    with tempfile.TemporaryDirectory(prefix="harness-media-") as directory:
        temp = Path(directory)
        metadata = temp / "metadata.json"
        try:
            with metadata.open("wb") as output:
                success = _command([probe, "-v", "error", *source, "-show_entries",
                                    "format=duration:stream=codec_type,codec_name,width,height,"
                                    "avg_frame_rate,sample_rate,channels", "-of", "json"], output=output)
            info = json.loads(_read_text(metadata)) if success else {}
        except (OSError, ValueError):
            info = {}
        if not info:
            return [_text(label + " · Media metadata is unavailable.")]
        blocks = [_text(label + "\n" + json.dumps(info, ensure_ascii=False, indent=2))]
        if not is_video or limit == 0:
            return blocks
        if not converter:
            return [*blocks, _text("Video frames require ffmpeg.")]
        try:
            duration = float(info.get("format", {}).get("duration", 0))
        except (TypeError, ValueError):
            duration = 0.0
        start = start if start is not None else 0.0
        if not math.isfinite(duration) or duration <= start:
            return [*blocks, _text("Video has no frames in the requested time range.")]
        finish = min(end, duration) if end is not None else duration
        take = min(limit, frames or (4 if automatic else 8))
        if fps is not None and (finish - start) * fps < take:
            take = math.ceil((finish - start) * fps)
        frame = temp / "frame.jpg"
        for index in range(take):
            instant = start + index / fps if fps else start + (finish - start) * (index + 0.5) / take
            frame.unlink(missing_ok=True)
            if _command([converter, "-v", "error", "-nostdin", "-y", "-ss", str(instant), *source,
                         "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1", "-vf",
                         f"scale={edge}:{edge}:force_original_aspect_ratio=decrease", str(frame)]) and frame.is_file():
                with Image.open(frame) as opened:
                    blocks.append(_image(opened, name.stem, edge, variant=f"-{instant:.2f}s"))
        if len(blocks) == 1:
            blocks.append(_text("No video frames could be rendered."))
        return blocks


def preview(path: Path, *, filename: str | None = None, detail: str = "auto",
            crop: tuple[int, int, int, int] | None = None,
            pages: str | None = None, overview: bool = False, sheet: str | None = None,
            frames: int | None = None, start: float | None = None, end: float | None = None,
            fps: float | None = None, automatic: bool = False, max_images: int = MAX_IMAGES) -> list[dict[str, Any]]:
    """Read a single file and return bounded model-ready text and image blocks."""
    if detail not in {"auto", "low", "high"} or not 0 <= max_images <= MAX_IMAGES:
        raise ValueError("Invalid preview detail or image limit.")
    if crop is not None and (len(crop) != 4 or min(crop[:2]) < 0 or min(crop[2:]) < 1):
        raise ValueError("Crop requires x,y,width,height with positive dimensions.")
    if frames is not None and not 1 <= frames <= MAX_IMAGES:
        raise ValueError(f"Select between 1 and {MAX_IMAGES} frames.")
    if any(number is not None and (not math.isfinite(number) or number < 0) for number in (start, end, fps)):
        raise ValueError("Video times and frame rate must be finite and nonnegative.")
    if (end is not None and end <= (start or 0)) or fps is not None and not 0 < fps <= 60:
        raise ValueError("End must follow start and frame rate must be greater than 0 and at most 60.")

    name = Path(filename) if filename else path
    path = path.resolve()
    suffix = name.suffix.lower()
    if crop and suffix not in _RASTER:
        raise ValueError("Crop is available for raster images.")
    if pages and suffix not in {".pdf", ".docx", ".pptx"}:
        raise ValueError("Page selection is available for PDF, Word and PowerPoint.")
    if sheet is not None and suffix != ".xlsx":
        raise ValueError("Sheet selection requires an XLSX workbook.")
    if (start is not None or end is not None or fps is not None) and suffix not in _VIDEO:
        raise ValueError("Time range and frame rate selection require a video.")
    if frames is not None and suffix not in _RASTER and suffix not in _VIDEO:
        raise ValueError("Frame selection requires an image or a video.")

    try:
        size = path.stat().st_size
        if not path.is_file():
            raise ValueError("Preview requires a regular file.")
        mime_type = mimetypes.guess_type(name.name)[0] or "application/octet-stream"
        if size > MAX_SOURCE_BYTES:
            return [_text(f"{name.name} · {mime_type} · {size:,} bytes · File exceeds the preview limit.")]
        edge = 640 if overview or detail == "low" else 3072 if detail == "high" else 1536
        if suffix in _RASTER:
            return _raster(path, name, edge, crop, max_images, frames, automatic)
        if suffix == ".svg":
            return _svg(path, name, edge, max_images)
        if suffix == ".pdf":
            return _pdf(path, name, edge, pages, max_images, automatic, overview)
        if suffix in {".docx", ".pptx", ".xlsx"}:
            blocks = _office(path, name, pages, sheet)
            if not automatic and max_images and suffix in {".docx", ".pptx"}:
                blocks.extend(_office_pages(path, edge, pages, max_images, overview))
            return blocks
        if suffix in _VIDEO or suffix in _AUDIO:
            return _media(path, name, edge, max_images, frames, start, end, fps, automatic)
        if suffix in {".csv", ".tsv"}:
            return _table(path, name)
        if suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                listing = "\n".join(f"{entry.filename} · {entry.file_size:,} bytes" for entry in entries[:80])
            return [_text(f"{name.name} · {len(entries)} entries\n{listing}")]
        if suffix in _TEXT or mime_type.startswith("text/") or suffix in {".json", ".xml"}:
            return [_text(f"{name.name} · {mime_type}\n{_read_text(path)}")]
        return [_text(f"{name.name} · {mime_type} · {size:,} bytes · Preview format unavailable.")]
    except (OSError, Image.DecompressionBombError, ET.ParseError,
            KeyError, zipfile.BadZipFile, csv.Error):
        return [_text(f"{name.name} · Preview could not read this file.")]
