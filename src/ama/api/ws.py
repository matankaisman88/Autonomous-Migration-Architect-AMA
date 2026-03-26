from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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

