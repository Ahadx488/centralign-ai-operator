from __future__ import annotations

import csv
import fnmatch
import json
import logging
import os
import tempfile
from typing import Any

from pydantic import BaseModel, Field
from pypdf import PdfReader

from core.tools.registry import BaseTool, RiskTier, ToolResult

logger = logging.getLogger(__name__)

# Allowed directories for sandboxed file operations
ALLOWED_ROOTS = [
    os.path.abspath("."),
    os.path.abspath("./env/fileshare"),
    os.path.abspath("./artifacts"),
    os.path.abspath("./data"),
    os.path.abspath(tempfile.gettempdir()),
    "/tmp",
    "/private/var/folders",
]


def resolve_safe_path(path: str) -> str:
    """Validate that path resides within allowed sandboxed roots."""
    abs_path = os.path.abspath(path)
    if not any(abs_path.startswith(root) for root in ALLOWED_ROOTS):
        raise PermissionError(f"Access denied: Path '{path}' is outside allowed sandboxed roots.")
    return abs_path


# ---------------------------------------------------------------------------
# File Tools Schemas & Implementations
# ---------------------------------------------------------------------------


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="Directory path to list")


class ListDirTool(BaseTool):
    name = "files_list_dir"
    description = "List contents of a directory (files and subdirectories) with metadata."
    args_schema = ListDirArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs.get("path", ".")
        safe_path = resolve_safe_path(path)
        if not os.path.exists(safe_path):
            return ToolResult.failure(f"Directory not found: {path}")
        if not os.path.isdir(safe_path):
            return ToolResult.failure(f"Path is not a directory: {path}")

        entries = []
        for name in sorted(os.listdir(safe_path)):
            full = os.path.join(safe_path, name)
            is_dir = os.path.isdir(full)
            size = os.path.getsize(full) if not is_dir else 0
            entries.append(
                {
                    "name": name,
                    "is_dir": is_dir,
                    "size_bytes": size,
                    "size_display": f"{size / 1024:.1f} KB" if not is_dir else "-",
                }
            )

        return ToolResult.success(data={"path": path, "entries": entries})


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path to read (supports PDF, CSV, JSON, TXT/Markdown)")


class ReadFileTool(BaseTool):
    name = "files_read_file"
    description = "Read and extract contents of a file (PDF text extraction, CSV parsing, JSON, TXT)."
    args_schema = ReadFileArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs["path"]
        safe_path = resolve_safe_path(path)
        if not os.path.exists(safe_path):
            return ToolResult.failure(f"File not found: {path}")
        if os.path.isdir(safe_path):
            return ToolResult.failure(f"Path is a directory, not a file: {path}")

        ext = os.path.splitext(safe_path)[1].lower()

        # 1. PDF Text Extraction via pypdf
        if ext == ".pdf":
            try:
                reader = PdfReader(safe_path)
                text_pages = [page.extract_text() or "" for page in reader.pages]
                full_text = "\n--- Page Break ---\n".join(text_pages).strip()
                return ToolResult.success(
                    data={
                        "format": "pdf",
                        "pages_count": len(reader.pages),
                        "content": full_text,
                    },
                    evidence_refs=[safe_path],
                )
            except Exception as e:
                return ToolResult.failure(f"Failed to extract text from PDF '{path}': {e}")

        # 2. CSV parsing
        if ext == ".csv":
            try:
                with open(safe_path, mode="r", encoding="utf-8", errors="replace") as f:
                    reader_csv = csv.DictReader(f)
                    rows = list(reader_csv)
                return ToolResult.success(
                    data={"format": "csv", "rows_count": len(rows), "rows": rows},
                    evidence_refs=[safe_path],
                )
            except Exception as e:
                return ToolResult.failure(f"Failed to parse CSV '{path}': {e}")

        # 3. JSON parsing
        if ext == ".json":
            try:
                with open(safe_path, mode="r", encoding="utf-8") as f:
                    data = json.load(f)
                return ToolResult.success(
                    data={"format": "json", "content": data},
                    evidence_refs=[safe_path],
                )
            except Exception as e:
                return ToolResult.failure(f"Failed to parse JSON '{path}': {e}")

        # 4. Standard Text / Markdown
        try:
            with open(safe_path, mode="r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return ToolResult.success(
                data={"format": "text", "content": content},
                evidence_refs=[safe_path],
            )
        except Exception as e:
            return ToolResult.failure(f"Failed to read file '{path}': {e}")


class WriteFileArgs(BaseModel):
    path: str = Field(description="File path to write")
    content: str = Field(description="String content to write to file")


class WriteFileTool(BaseTool):
    name = "files_write_file"
    description = "Write text content to a file in the workspace or artifacts directory."
    args_schema = WriteFileArgs
    risk_tier = RiskTier.REVERSIBLE_WRITE

    async def _run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs["path"]
        content: str = kwargs["content"]
        safe_path = resolve_safe_path(path)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)

        with open(safe_path, mode="w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult.success(
            data={"path": path, "size_bytes": len(content)},
            evidence_refs=[safe_path],
        )


class SearchFilesArgs(BaseModel):
    pattern: str = Field(description="Glob search pattern (e.g. '*.pdf', '*statement*')")
    base_dir: str = Field(default=".", description="Starting directory to search")


class SearchFilesTool(BaseTool):
    name = "files_search_files"
    description = "Recursively search for files matching a glob pattern."
    args_schema = SearchFilesArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        pattern: str = kwargs["pattern"]
        base_dir: str = kwargs.get("base_dir", ".")
        safe_base = resolve_safe_path(base_dir)
        matches: list[dict[str, Any]] = []

        for root, _, files in os.walk(safe_base):
            for filename in files:
                if fnmatch.fnmatch(filename, pattern):
                    full = os.path.join(root, filename)
                    rel = os.path.relpath(full, safe_base)
                    matches.append(
                        {
                            "filename": filename,
                            "relative_path": rel,
                            "full_path": full,
                            "size_bytes": os.path.getsize(full),
                        }
                    )

        return ToolResult.success(data={"pattern": pattern, "matches": matches})
