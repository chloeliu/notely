"""File detection, text extraction, and attachment management."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Extensions we can handle
TEXT_EXTENSIONS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
                   ".toml", ".csv", ".html", ".css", ".xml", ".sh", ".bash",
                   ".rb", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".sql",
                   ".log", ".ini", ".cfg", ".conf", ".env", ".gitignore"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | PDF_EXTENSIONS | IMAGE_EXTENSIONS


def is_file_path(text: str) -> Path | None:
    """Check if text is a single-line file path that exists on disk.

    Returns the resolved Path if it's a real file, None otherwise.
    Avoids false positives on slash commands (e.g. /help).
    """
    text = text.strip()

    # Must be a single line (check both \n and \r for pasted text)
    if "\n" in text or "\r" in text:
        return None

    # Skip very short strings or obvious non-paths
    if len(text) < 3:
        return None

    # OS path length limit — avoid OSError on long pastes
    if len(text) > 1024:
        return None

    # Expand ~ to home directory
    expanded = Path(text).expanduser()

    # Must exist and be a file (not a directory)
    try:
        if expanded.is_file():
            return expanded.resolve()
    except OSError:
        # Path too long or other OS-level path error
        return None

    return None


def extract_text(file_path: Path) -> tuple[str, str]:
    """Extract text content from a file.

    Returns (extracted_text, file_type) where file_type is one of:
    "text", "pdf", "image", "unknown".

    For unsupported types, returns a placeholder message.
    """
    suffix = file_path.suffix.lower()

    if suffix in TEXT_EXTENSIONS:
        try:
            text = file_path.read_text(encoding="utf-8")
            return text, "text"
        except UnicodeDecodeError:
            return f"[Binary file: {file_path.name}]", "unknown"

    if suffix in PDF_EXTENSIONS:
        return _extract_pdf(file_path), "pdf"

    if suffix in IMAGE_EXTENSIONS:
        # Try Vision API for a real description
        try:
            from .ai import describe_image
            description = describe_image(file_path)
            if description:
                return description, "image"
        except Exception:
            pass
        return f"[Image: {file_path.name} ({_human_size(file_path.stat().st_size)})]", "image"

    # Unknown extension — try reading as text
    try:
        text = file_path.read_text(encoding="utf-8")
        return text, "text"
    except (UnicodeDecodeError, Exception):
        return f"[File: {file_path.name} ({_human_size(file_path.stat().st_size)})]", "unknown"


def copy_attachment(
    file_path: Path,
    config: "NotelyConfig",
    space: str,
    group_slug: str,
    subgroup_slug: str | None = None,
) -> str:
    """Copy a file into the attachments/ directory, mirroring the note folder structure.

    The destination path mirrors how notes are organized:
    attachments/<space>/<group_slug>/[<subgroup_slug>/]<filename>

    If a file with the same name already exists at the destination,
    numeric suffixes are appended to avoid overwriting: report_1.pdf,
    report_2.pdf, etc. The original file is preserved (copied, not moved).

    Args:
        file_path: Absolute path to the source file to copy.
        config: NotelyConfig instance (used for base_dir to compute relative paths).
        space: Space name (e.g. "clients").
        group_slug: Group slug (e.g. "acme-corp").
        subgroup_slug: Optional subgroup slug (e.g. "api-project").

    Returns:
        Relative path string from the workspace root, e.g.
        "attachments/clients/acme-corp/report.pdf".
    """
    if subgroup_slug:
        dest_dir = config.base_dir / "attachments" / space / group_slug / subgroup_slug
    else:
        dest_dir = config.base_dir / "attachments" / space / group_slug
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / file_path.name
    # Handle name collisions
    if dest.exists():
        stem = file_path.stem
        suffix = file_path.suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    shutil.copy2(file_path, dest)
    return str(dest.relative_to(config.base_dir))


def _extract_pdf(file_path: Path) -> str:
    """Extract text from a PDF, preferring pymupdf4llm for markdown with tables.

    Two-tier strategy:
    1. pymupdf4llm.to_markdown() — clean markdown with tables/structure
    2. Raw pymupdf page.get_text() — plain text fallback
    If neither is installed, returns a placeholder with install instructions.
    """
    # Tier 1: pymupdf4llm (markdown with tables)
    try:
        import pymupdf4llm
        try:
            md_text = pymupdf4llm.to_markdown(str(file_path))
            if md_text and md_text.strip():
                return md_text
        except Exception:
            pass  # Fall through to tier 2
    except ImportError:
        pass

    # Tier 2: raw pymupdf (plain text)
    try:
        import pymupdf
    except ImportError:
        return (
            f"[PDF: {file_path.name} — install pymupdf4llm for text extraction: "
            f"pip install pymupdf4llm]"
        )

    try:
        doc = pymupdf.open(str(file_path))
        pages = []
        for page in doc:
            text = page.get_text()
            if text.strip():
                pages.append(text)
        doc.close()
        if pages:
            return "\n\n---\n\n".join(pages)
        return f"[PDF: {file_path.name} — no extractable text (scanned/image PDF)]"
    except Exception as e:
        return f"[PDF: {file_path.name} — extraction failed: {e}]"


def _human_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.0f}{unit}" if unit == "B" else f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"
