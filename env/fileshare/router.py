from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from env.faults.manager import fault_manager
from env.seed.generator import DATA_DIR

router = APIRouter(prefix="/files", tags=["Shared Files"])
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)


@router.get("/browse")
async def fileshare_browse(request: Request, subpath: str = "") -> Response:
    await fault_manager.maybe_delay(request.url.path)
    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    target_dir = os.path.abspath(os.path.join(DATA_DIR, subpath.strip("/")))
    if not target_dir.startswith(os.path.abspath(DATA_DIR)):
        raise HTTPException(status_code=403, detail="Access denied: outside fileshare root")

    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    items = []
    for entry in sorted(os.listdir(target_dir)):
        full = os.path.join(target_dir, entry)
        is_dir = os.path.isdir(full)
        size_bytes = os.path.getsize(full) if not is_dir else 0
        size_str = f"{size_bytes / 1024:.1f} KB" if not is_dir else "-"
        ext = os.path.splitext(entry)[1].upper().replace(".", "") or ("DIR" if is_dir else "FILE")
        rel = os.path.relpath(full, DATA_DIR)

        items.append(
            {
                "name": entry,
                "is_dir": is_dir,
                "size_str": size_str,
                "type": ext,
                "rel_path": rel,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="fileshare/browse.html",
        context={
            "title": "Company Shared Drive",
            "app_name": "Company Shared Drive",
            "system_name": "Fileshare",
            "current_path": f"./env/fileshare/data/{subpath}",
            "items": items,
        },
    )


@router.get("/download/{filename:path}")
async def fileshare_download(filename: str) -> Response:
    safe_path = os.path.abspath(os.path.join(DATA_DIR, filename))
    if not safe_path.startswith(os.path.abspath(DATA_DIR)):
        raise HTTPException(status_code=403, detail="Access denied: outside fileshare root")
    if not os.path.exists(safe_path) or os.path.isdir(safe_path):
        raise HTTPException(status_code=404, detail="File not found")

    media_type = "application/pdf" if filename.endswith(".pdf") else "application/octet-stream"
    return FileResponse(safe_path, media_type=media_type, filename=os.path.basename(safe_path))
