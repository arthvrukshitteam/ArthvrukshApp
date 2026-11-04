"""Bulk Fund Analysis API — mounted at /api/bulk-analysis by server.py.

HTTP layer only. All matching/calculation orchestration lives in
bulk_analysis.py; this module parses uploads, manages job documents, and
returns the processed workbook. It receives the app's existing analysis
builder/cache-lookup as plain callables from server.py — same
"no DI framework, db/helpers passed explicitly" convention proposal_routes.py
uses — so it never re-implements FQS/XIRR and never needs to import
server.py (circular, since server.py imports this module's router factory).

Flow:
    POST   /upload            -> multipart upload (web, curl) — parse +
                                  preview, does NOT compute anything
    POST   /upload-base64     -> JSON {filename, content_base64} — same
                                  result, for React Native native builds (see
                                  below)
    POST   /{job_id}/process  -> kicks off the background batch job
    GET    /{job_id}/status   -> progress counters (real, not simulated)
    GET    /{job_id}/download -> the processed .xlsx, once status=="completed"

Two upload routes exist because RN's native file-upload story has proven
unreliable across environments during testing: the classic FormData
{uri,name,type} trick throws "Unsupported FormDataPart implementation" under
the new architecture/Hermes, and expo-file-system's uploadAsync has thrown
an IOException reading its own app's DocumentPicker cache file in Expo Go.
Base64-over-JSON sidesteps all native multipart/file-reading machinery —
it's a plain string in a JSON body, identical code path to every other
request this client already makes successfully. Web keeps using real
multipart (a real Blob/File there works fine and avoids a ~33% payload
bump), native uses the base64 route — see PortfolioReviewScreen.uploadFile().
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

import bulk_analysis as ba

logger = logging.getLogger("mf_api.bulk_analysis")

ALLOWED_EXTENSIONS = {".xlsx", ".xls"}

# In-process task handles, keyed by job_id — same lightweight pattern as
# server.py's _fqs_refresh_task/_lumpsum_refresh_task (no queue framework;
# this app doesn't need one for jobs of this size).
_running_tasks: Dict[str, "asyncio.Task"] = {}


async def recover_orphaned_jobs(db) -> int:
    """Mark jobs left at status "processing" by a previous process as failed.

    Jobs run as in-process asyncio tasks, so a restart (deploy, free-tier
    spin-down, crash) kills them while their Mongo document still says
    "processing" — and the UI would poll that forever. Called once at startup,
    when by definition no task of this process exists yet. The uploaded
    workbook is kept (it is only removed on completion), so the user can
    simply press Process again.
    """
    res = await db[ba.JOBS_COLLECTION].update_many(
        {"status": "processing"},
        {"$set": {
            "status": "failed",
            "error": "Interrupted by a server restart — press Process to run it again.",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return res.modified_count


class Base64UploadIn(BaseModel):
    filename: str
    content_base64: str


def build_router(db, build_analysis_fn: ba.BuildAnalysisFn, cached_analysis_fn: ba.CachedAnalysisFn) -> APIRouter:
    router = APIRouter(prefix="/api/bulk-analysis", tags=["bulk-analysis"])
    jobs = db[ba.JOBS_COLLECTION]

    async def _finish_upload(raw_name: str, content: bytes) -> Dict[str, Any]:
        """Shared by both upload routes once they've each produced raw file
        bytes — extension check, size check, .xls conversion, preview parse,
        job-doc creation. Identical result regardless of transport."""
        filename = ba.safe_filename(raw_name or "upload.xlsx")
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Only .xlsx and .xls files are supported.")

        if not content:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        if len(content) > ba.MAX_UPLOAD_BYTES:
            mb = ba.MAX_UPLOAD_BYTES // (1024 * 1024)
            raise HTTPException(status_code=400, detail=f"File too large (max {mb}MB).")

        # openpyxl/xlrd parsing is CPU-bound and fully synchronous. Run
        # directly, it blocks the event loop for the whole parse — on a large
        # workbook that stalls EVERY other request the process is serving, not
        # just this one. A worker thread keeps the server responsive; the work
        # itself is unchanged.
        if ext == ".xls":
            try:
                content = await asyncio.to_thread(ba.convert_xls_to_xlsx_bytes, content)
            except Exception:
                logger.exception("Bulk analysis: .xls conversion failed for %s", filename)
                raise HTTPException(
                    status_code=400,
                    detail="Couldn't read this legacy .xls file — please re-save it as "
                           ".xlsx in Excel and re-upload.",
                )

        try:
            preview = await asyncio.to_thread(ba.parse_preview, content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            logger.exception("Bulk analysis: couldn't parse %s", filename)
            raise HTTPException(status_code=400, detail="Couldn't read this file as an Excel workbook.")

        job_id = str(uuid.uuid4())
        await jobs.insert_one({
            "_id": job_id,
            "status": "pending",
            "filename": filename,
            "total": preview["total"],
            "processed": 0,
            "matched": 0,
            "not_found": 0,
            "errors": 0,
            "preview_funds": preview["preview_funds"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "workbook_bytes": content,
        })
        return {
            "job_id": job_id,
            "filename": filename,
            "total_funds": preview["total"],
            "detected_funds": preview["preview_funds"],
        }

    @router.post("/upload")
    async def upload(file: UploadFile = File(...)):
        content = await file.read()
        return await _finish_upload(file.filename or "upload.xlsx", content)

    @router.post("/upload-base64")
    async def upload_base64(body: Base64UploadIn):
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="content_base64 is not valid base64.")
        return await _finish_upload(body.filename, content)

    @router.post("/{job_id}/process")
    async def process(job_id: str):
        doc = await jobs.find_one({"_id": job_id}, {"status": 1, "workbook_bytes": 1})
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")
        # Only a live task of THIS process counts as running. A document that
        # merely says "processing" is an orphan from a dead process (see
        # recover_orphaned_jobs) and may be restarted.
        if job_id in _running_tasks and not _running_tasks[job_id].done():
            return {"status": "already_running"}
        if doc.get("status") == "completed":
            return {"status": "already_completed"}
        if not doc.get("workbook_bytes"):
            raise HTTPException(status_code=409, detail="Nothing to process — upload a file first.")

        await jobs.update_one(
            {"_id": job_id},
            {"$set": {"status": "processing", "processed": 0, "matched": 0,
                      "not_found": 0, "errors": 0, "finished_at": None},
             "$unset": {"error": ""}},
        )
        workbook_bytes = bytes(doc["workbook_bytes"])
        # The job document (with its own copy of the upload) is not needed once
        # the bytes are extracted; on a 10MB workbook holding both references
        # for the life of the request doubles this endpoint's peak RSS.
        doc.clear()
        task = asyncio.create_task(
            ba.process_bulk_job(job_id, db, workbook_bytes, build_analysis_fn, cached_analysis_fn)
        )
        _running_tasks[job_id] = task
        # Finished handles are dropped as soon as the task completes. Without
        # this the dict grows for the life of the process — one entry per
        # upload ever processed, each pinning a completed Task and, through
        # its frame, whatever that job still referenced.
        task.add_done_callback(lambda _t, jid=job_id: _running_tasks.pop(jid, None))
        del workbook_bytes
        return {"status": "started"}

    @router.get("/{job_id}/status")
    async def status(job_id: str):
        doc = await jobs.find_one({"_id": job_id}, {"output_bytes": 0, "workbook_bytes": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")
        doc["job_id"] = doc.pop("_id")
        return doc

    @router.get("/{job_id}/download")
    async def download(job_id: str):
        doc = await jobs.find_one({"_id": job_id}, {"output_bytes": 1, "status": 1, "filename": 1})
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")
        if doc.get("status") != "completed" or not doc.get("output_bytes"):
            raise HTTPException(status_code=409, detail="This job hasn't finished processing yet.")

        base = (doc.get("filename") or "funds").rsplit(".", 1)[0]
        out_name = ba.safe_filename(f"{base}_analyzed.xlsx")
        return Response(
            content=bytes(doc["output_bytes"]),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )

    return router
