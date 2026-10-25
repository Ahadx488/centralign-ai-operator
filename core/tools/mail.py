from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from pydantic import BaseModel, Field

from core.tools.registry import BaseTool, RiskTier, ToolResult
from env.db import query_all, query_one
from env.seed.generator import INVOICES_DIR

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = os.getenv("ARTIFACTS_DIR", os.path.abspath("./artifacts"))
DOWNLOADS_DIR = os.path.join(ARTIFACTS_DIR, "downloads")


class SearchMailArgs(BaseModel):
    query: str = Field(default="", description="Search keyword for sender, subject, or email body")


class SearchMailTool(BaseTool):
    name = "mail_search"
    description = "Search incoming emails in the corporate mailbox by sender, subject, or keyword."
    args_schema = SearchMailArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        query: str = kwargs.get("query", "")
        sql = "SELECT id, sender, recipient, subject, attachment_filename, date FROM emails"
        params: list[str] = []
        if query.strip():
            sql += " WHERE sender LIKE ? OR subject LIKE ? OR body LIKE ?"
            wildcard = f"%{query.strip()}%"
            params.extend([wildcard, wildcard, wildcard])
        sql += " ORDER BY id DESC"

        emails = query_all(sql, tuple(params))
        return ToolResult.success(data={"count": len(emails), "emails": emails})


class ReadMailArgs(BaseModel):
    email_id: int = Field(description="ID of the email to retrieve and read")


class ReadMailTool(BaseTool):
    name = "mail_read"
    description = "Read full details and message body of a specific email by ID."
    args_schema = ReadMailArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        email_id: int = kwargs["email_id"]
        email = query_one("SELECT * FROM emails WHERE id = ?", (email_id,))
        if not email:
            return ToolResult.failure(f"Email with ID {email_id} not found.")

        return ToolResult.success(
            data={"email": email},
            evidence_refs=[f"email:{email_id}"],
        )


class SaveAttachmentArgs(BaseModel):
    email_id: int = Field(description="ID of the email with the attachment to download")
    save_dir: str | None = Field(default=None, description="Directory to save attachment (default: artifacts/downloads)")


class SaveAttachmentTool(BaseTool):
    name = "mail_save_attachment"
    description = "Download and save an email attachment to local disk."
    args_schema = SaveAttachmentArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        email_id: int = kwargs["email_id"]
        save_dir: str | None = kwargs.get("save_dir")

        email = query_one("SELECT * FROM emails WHERE id = ?", (email_id,))
        if not email:
            return ToolResult.failure(f"Email with ID {email_id} not found.")

        filename = email.get("attachment_filename")
        if not filename:
            return ToolResult.failure(f"Email #{email_id} has no attachment.")

        source_path = os.path.join(INVOICES_DIR, filename)
        if not os.path.exists(source_path):
            return ToolResult.failure(f"Attachment file '{filename}' was not found on disk.")

        target_dir = save_dir or DOWNLOADS_DIR
        os.makedirs(target_dir, exist_ok=True)
        dest_path = os.path.join(target_dir, filename)
        shutil.copyfile(source_path, dest_path)

        return ToolResult.success(
            data={"saved_path": dest_path, "filename": filename, "size_bytes": os.path.getsize(dest_path)},
            evidence_refs=[dest_path],
        )
