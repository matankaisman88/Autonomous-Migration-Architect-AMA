from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ama.api.live_jobs import live_job_snapshot
from ama.bulk_runner import _BULK_JOBS, _BULK_JOBS_LOCK

router = APIRouter()


@router.websocket("/ws/bulk/{job_id}")
async def ws_bulk_progress(websocket: WebSocket, job_id: str) -> None:
    """Stream bulk job status snapshots until completion or disconnect."""
    await websocket.accept()
    try:
        with _BULK_JOBS_LOCK:
            state = _BULK_JOBS.get(job_id)
            if not isinstance(state, dict):
                await websocket.send_json({"error": "job not found"})
                await websocket.close()
                return
            payload = dict(state)
        await websocket.send_json(payload)

        while str(payload.get("status") or "") in {"queued", "running"}:
            await asyncio.sleep(0.8)
            with _BULK_JOBS_LOCK:
                state = _BULK_JOBS.get(job_id)
                if not isinstance(state, dict):
                    await websocket.send_json({"error": "job not found"})
                    await websocket.close()
                    return
                payload = dict(state)
            await websocket.send_json(payload)

        await websocket.send_json(payload)
        await websocket.close()
    except WebSocketDisconnect:
        return


@router.websocket("/ws/live/{job_id}")
async def ws_live_progress(websocket: WebSocket, job_id: str) -> None:
    """Stream live ingestion job snapshots (stage, percent, log_lines) until terminal status."""
    await websocket.accept()
    try:
        snap = live_job_snapshot(job_id)
        if snap is None:
            await websocket.send_json({"error": "job not found"})
            await websocket.close()
            return
        payload = {
            "stage": snap.get("stage", ""),
            "percent": int(snap.get("percent") or 0),
            "log_lines": snap.get("log_lines") or [],
            "status": snap.get("status", ""),
            "errors": snap.get("errors") or [],
            "build_report": snap.get("build_report"),
            "report_path": snap.get("report_path"),
            "report_build_error": snap.get("report_build_error"),
        }
        await websocket.send_json(payload)

        while str(payload.get("status") or "") in {"queued", "running"}:
            await asyncio.sleep(0.8)
            snap = live_job_snapshot(job_id)
            if snap is None:
                await websocket.send_json({"error": "job not found"})
                await websocket.close()
                return
            payload = {
                "stage": snap.get("stage", ""),
                "percent": int(snap.get("percent") or 0),
                "log_lines": snap.get("log_lines") or [],
                "status": snap.get("status", ""),
                "errors": snap.get("errors") or [],
                "build_report": snap.get("build_report"),
                "report_path": snap.get("report_path"),
                "report_build_error": snap.get("report_build_error"),
            }
            await websocket.send_json(payload)

        await websocket.send_json(payload)
        await websocket.close()
    except WebSocketDisconnect:
        return

