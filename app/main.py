"""FastAPI 入口：录像分片接收 + 游标续读回看。

接口一览：
  POST /v1/segments/{seq}?independent=true|false   上传一片（原始字节，<=16 MiB）
  GET  /v1/playback/start?seq=N                    起播，返回游标
  GET  /v1/playback/read?cursor=...&max_bytes=...  凭游标续读，每次 <=64 KiB 原始字节
  PUT  /v1/slots/limit?limit=N                     在线调小/调大活动槽位上限
  POST /v1/slots/reclaim                            立即重试删除待删旧片
  GET  /v1/state                                   槽位/容量/回收现状（演示与排障用）
  GET  /healthz                                    健康检查

状态语义：
  200 正常（续读时响应体即原始字节，下一游标在 X-Next-Cursor 响应头）
  204 等待（无可用起点 / 下一片尚未写入，原因在 X-Reason 响应头）
  410 断档（起点或续读位置已被覆盖/淘汰，JSON 里给出 earliest_playable）
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from .config import Settings
from .store import (
    BadCursor,
    EmptySegment,
    Gap,
    InvalidSlotLimit,
    NonContiguous,
    ReadOk,
    ReclaimIncomplete,
    SegmentStore,
    StartOk,
)

REASON_NO_START = "no-independent-start"
REASON_NO_NEXT = "next-segment-not-written"


def create_app(settings: Settings, unlink_fn=None) -> FastAPI:
    store = SegmentStore(
        data_dir=settings.data_dir,
        slot_count=settings.slot_count,
        max_segment_bytes=settings.max_segment_bytes,
        max_read_bytes=settings.max_read_bytes,
        unlink_fn=unlink_fn,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # 停止后台回收任务；待删文件留在磁盘/trash，下次启动统一清空。
        await store.aclose()

    app = FastAPI(title="segment-recorder", version="0.2.0", lifespan=lifespan)
    app.state.store = store

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/segments/{seq}", status_code=201)
    async def upload_segment(
        request: Request,
        seq: int = Path(ge=0),
        independent: bool = Query(False),
    ):
        # 先信 Content-Length 快速拒绝，再边读边限长，避免超限片占内存。
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit():
            if int(declared) > store.max_segment_bytes:
                return _too_large(store.max_segment_bytes)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > store.max_segment_bytes:
                return _too_large(store.max_segment_bytes)
        try:
            result = await store.put(seq=seq, independent=independent, data=bytes(body))
        except EmptySegment:
            return JSONResponse(status_code=400, content={"detail": "empty segment body"})
        except NonContiguous as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": str(exc),
                    "expected_seq": exc.expected,
                    "got_seq": exc.got,
                },
            )
        return {
            "seq": result.seq,
            "slot": result.slot,
            "generation": result.generation,
            "size": result.size,
            "independent": result.independent,
            "oldest_seq": result.oldest_seq,
            "newest_seq": result.newest_seq,
            "slot_limit": result.slot_limit,
        }

    @app.get("/v1/playback/start")
    async def playback_start(seq: int = Query(ge=0)):
        result = await store.start(seq)
        if isinstance(result, StartOk):
            return {
                "status": "ok",
                "start_seq": result.start_seq,
                "cursor": result.cursor.encode(),
            }
        if isinstance(result, Gap):
            return JSONResponse(
                status_code=410,
                content={
                    "status": "gap",
                    "detail": "requested start point has been overwritten",
                    "earliest_playable": result.earliest_playable,
                },
            )
        return Response(status_code=204, headers={"X-Reason": REASON_NO_START})

    @app.get("/v1/playback/read")
    async def playback_read(
        cursor: str = Query(...),
        max_bytes: int = Query(
            default=settings.max_read_bytes, ge=1, le=settings.max_read_bytes
        ),
    ):
        try:
            result = await store.read(cursor, max_bytes)
        except BadCursor as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        if isinstance(result, ReadOk):
            end_offset = result.offset + len(result.data)
            headers = {
                "X-Next-Cursor": result.next_cursor.encode(),
                "X-Seq": str(result.seq),
                "X-Offset": str(result.offset),
                "X-Segment-Size": str(result.segment_size),
                "X-Independent": "true" if result.independent else "false",
                "X-End-Of-Segment": "true" if end_offset >= result.segment_size else "false",
            }
            return Response(
                content=result.data,
                media_type="application/octet-stream",
                headers=headers,
            )
        if isinstance(result, Gap):
            return JSONResponse(
                status_code=410,
                content={
                    "status": "gap",
                    "detail": "segment was overwritten before it could be read",
                    "earliest_playable": result.earliest_playable,
                },
            )
        return Response(status_code=204, headers={"X-Reason": REASON_NO_NEXT})

    # limit 由 store 校验并返回带范围的 422，因此这里不用 Query(ge=1)。
    @app.put("/v1/slots/limit")
    async def set_slot_limit(limit: int = Query(...)):
        try:
            result = await store.set_slot_limit(limit)
        except InvalidSlotLimit as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": str(exc),
                    "limit": exc.limit,
                    "min_limit": 1,
                    "configured_slot_count": exc.configured,
                },
            )
        except ReclaimIncomplete as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "reclaim-incomplete",
                    "detail": str(exc),
                    "pending_delete_bytes": exc.pending_bytes,
                    "pending_segments": exc.pending_segments,
                    "retry_endpoint": "POST /v1/slots/reclaim",
                },
            )
        snapshot = await store.snapshot()
        return {
            "status": "ok",
            "previous_limit": result.previous_limit,
            "slot_limit": result.slot_limit,
            "configured_slot_count": store.slot_count,
            "retained_seqs": result.retained_seqs,
            "evicted_seqs": result.evicted_seqs,
            "activated_slots": result.activated_slots,
            "evicted_delete_bytes": result.pending_delete_bytes,
            "pending_delete_bytes": snapshot["pending_delete_bytes"],
            "reclaim_complete": snapshot["reclaim_complete"],
            "reclaim": snapshot["reclaim"],
            "state": snapshot,
        }

    @app.post("/v1/slots/reclaim")
    async def reclaim_slots():
        snapshot = await store.retry_reclaim()
        return {"status": "ok", "reclaim": snapshot["reclaim"], "state": snapshot}

    @app.get("/v1/state")
    async def state():
        return await store.snapshot()

    @app.get("/")
    async def root():
        return {
            "service": "segment-recorder",
            "endpoints": [
                "POST /v1/segments/{seq}?independent=",
                "GET /v1/playback/start?seq=",
                "GET /v1/playback/read?cursor=&max_bytes=",
                "PUT /v1/slots/limit?limit=",
                "POST /v1/slots/reclaim",
                "GET /v1/state",
                "GET /healthz",
            ],
        }

    return app


def _too_large(limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={"detail": f"segment exceeds {limit} bytes (16 MiB) limit"},
    )


# uvicorn app.main:app 使用的模块级实例（测试走 create_app 自定义配置）。
app = create_app(Settings.from_env())
