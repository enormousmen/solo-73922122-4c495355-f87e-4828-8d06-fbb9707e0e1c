"""自测：在线调容（调小/调大槽位上限）、淘汰、回收与旧清理安全。

覆盖需求要点：
- 上限可取 1..启动配置槽位数，越界 422。
- 缩到 N：留下生效时最新的至多 N 片；后续上传按新上限覆盖。
- 保留片原游标从原偏移续读；淘汰片游标报 410 断档。
- 调容期间不复制录像（保留片文件名/内容/代次/槽位不变）。
- 删除旧片时：上传、回看、调大上限照常；已淘汰的片不恢复；
  旧清理不会删掉复用槽位的新片。
- 状态报告待删字节；删除失败可重试；删完才报回收完成。
- 回收未完时拒绝再次调小（调大仍允许）。
"""
import asyncio

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import Gap, ReadOk, SegmentStore

SLOT_COUNT = 4
SEG = 100 * 1024


def seg_bytes(seq: int, size: int = SEG) -> bytes:
    return bytes([seq % 256]) * size


def make_client(tmp_path, slot_count: int = SLOT_COUNT, unlink_fn=None):
    app = create_app(
        Settings(slot_count=slot_count, data_dir=str(tmp_path / "data")),
        unlink_fn=unlink_fn,
    )
    return app, TestClient(app)


@pytest.fixture()
def client(tmp_path):
    app, c = make_client(tmp_path)
    with c:
        yield c


def upload(client: TestClient, seq: int, independent: bool = False, size: int = SEG):
    return client.post(
        f"/v1/segments/{seq}",
        params={"independent": str(independent).lower()},
        content=seg_bytes(seq, size),
    )


def resize(client: TestClient, limit: int):
    return client.put("/v1/slots/limit", params={"limit": limit})


def cursor_for(client: TestClient, seq: int) -> str:
    r = client.get("/v1/playback/start", params={"seq": seq})
    assert r.status_code == 200, r.text
    return r.json()["cursor"]


def read(client: TestClient, cursor: str, **params):
    return client.get("/v1/playback/read", params={"cursor": cursor, **params})


def read_all(client: TestClient, cursor: str):
    buf = bytearray()
    while True:
        r = read(client, cursor)
        if r.status_code != 200:
            return bytes(buf), r
        buf += r.content
        cursor = r.headers["x-next-cursor"]


def wait_reclaim(client: TestClient, timeout: float = 2.0) -> dict:
    """轮询 /v1/state 直到回收完成。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get("/v1/state").json()
        if st["reclaim_complete"]:
            return st
        time.sleep(0.01)
    raise AssertionError(f"reclaim did not complete: {client.get('/v1/state').json()}")


# ---- 调小：保留最新至多 N 片 ----


def test_shrink_keeps_newest_n_and_reports(client):
    for s in range(1, 7):  # 4 槽环形 -> 留存 3,4,5,6
        assert upload(client, s, independent=(s == 6)).status_code == 201
    r = resize(client, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["previous_limit"] == 4 and body["slot_limit"] == 2
    assert body["retained_seqs"] == [5, 6]
    assert body["evicted_seqs"] == [3, 4]
    assert body["evicted_delete_bytes"] == 2 * SEG
    st = wait_reclaim(client)
    assert st["slot_limit"] == 2
    assert st["oldest_seq"] == 5 and st["newest_seq"] == 6
    assert st["pending_delete_bytes"] == 0
    active = [s for s in st["slots"] if s["active"]]
    assert sorted(s["seq"] for s in active) == [5, 6]


def test_shrink_keeps_newest_even_if_fewer_segments_than_limit(client):
    upload(client, 1)
    upload(client, 2)
    r = resize(client, 3)  # 只有 2 片，上限 3：两片都留
    assert r.status_code == 200
    assert r.json()["retained_seqs"] == [1, 2]
    assert r.json()["evicted_seqs"] == []
    st = wait_reclaim(client)
    assert st["slot_limit"] == 3
    assert st["oldest_seq"] == 1 and st["newest_seq"] == 2


def test_shrink_to_one_keeps_only_latest(client):
    for s in range(1, 5):
        upload(client, s, independent=(s in (1, 4)))
    r = resize(client, 1)
    assert r.status_code == 200
    assert r.json()["retained_seqs"] == [4]
    assert r.json()["evicted_seqs"] == [1, 2, 3]
    st = wait_reclaim(client)
    assert st["oldest_seq"] == st["newest_seq"] == 4
    assert st["earliest_playable"] == 4


# ---- 上限取值范围 ----


@pytest.mark.parametrize("limit", [0, -1, SLOT_COUNT + 1, 99])
def test_limit_out_of_range_rejected(client, limit):
    r = resize(client, limit)
    assert r.status_code == 422
    body = r.json()
    assert body["min_limit"] == 1 and body["configured_slot_count"] == SLOT_COUNT


def test_limit_upper_bound_is_startup_slot_count(client):
    assert resize(client, SLOT_COUNT).status_code == 200
    assert resize(client, 1).status_code == 200


# ---- 保留片原游标续读；淘汰片报断档 ----


def test_retained_cursor_continues_from_same_offset(client):
    for s in range(1, 7):
        upload(client, s, independent=(s == 6), size=SEG)
    # 从片 6 起播，先读一部分留下游标
    cursor = cursor_for(client, 6)
    r = read(client, cursor, max_bytes=1000)
    assert r.status_code == 200 and len(r.content) == 1000
    mid = r.headers["x-next-cursor"]

    resp = resize(client, 2)  # 保留 5、6，淘汰 3、4
    assert resp.status_code == 200
    wait_reclaim(client)

    # 原游标原偏移续读，内容与从 1000 偏移处开始一致
    r2 = read(client, mid)
    assert r2.status_code == 200, r2.text
    assert int(r2.headers["x-offset"]) == 1000
    assert int(r2.headers["x-seq"]) == 6
    # 后续接着读到直播边缘，拼接校验
    rest, last = read_all(client, r2.headers["x-next-cursor"])
    assert last.status_code == 204
    assert r.content + r2.content + rest == seg_bytes(6)


def test_evicted_cursor_reports_gap(client):
    for s in range(1, 5):
        upload(client, s, independent=True)
    cursor = cursor_for(client, 2)  # 片 2 可独立解码，先读到一半
    r = read(client, cursor, max_bytes=1000)
    assert r.status_code == 200
    mid = r.headers["x-next-cursor"]

    resize(client, 2)  # 保留 3、4，淘汰 1、2
    wait_reclaim(client)

    g = read(client, mid)
    assert g.status_code == 410
    assert g.json()["status"] == "gap"
    assert g.json()["earliest_playable"] == 3


def test_start_on_evicted_seq_reports_gap(client):
    for s in range(1, 5):
        upload(client, s, independent=(s == 4))
    resize(client, 2)
    wait_reclaim(client)
    g = client.get("/v1/playback/start", params={"seq": 1})
    assert g.status_code == 410
    assert g.json()["earliest_playable"] == 4


def test_cursor_end_of_evicted_segment_rolls_over_evicted_next(client):
    """淘汰片读完后滚到已被淘汰的下一片，也报断档而不是等下一片。"""
    for s in range(1, 5):
        upload(client, s, independent=(s in (2, 4)), size=1024)
    cursor = cursor_for(client, 2)
    r = read(client, cursor)
    assert r.status_code == 200 and r.headers["x-end-of-segment"] == "true"
    end = r.headers["x-next-cursor"]
    resize(client, 1)  # 只留片 4，淘汰 2、3
    wait_reclaim(client)
    g = read(client, end)
    assert g.status_code == 410
    assert g.json()["earliest_playable"] == 4  # 独立片 4 仍在


# ---- 缩容后继续录制：按新上限覆盖 ----


def test_upload_continues_with_new_limit(client):
    for s in range(1, 7):
        upload(client, s, independent=(s == 6))
    resize(client, 2)
    wait_reclaim(client)
    # 新上限 2：再传 2 片刚好填满/覆盖；按新上限只留 2 片
    assert upload(client, 7, independent=True).status_code == 201
    assert upload(client, 8).status_code == 201
    st = wait_reclaim(client)
    seqs = sorted(s["seq"] for s in st["slots"] if not s["empty"])
    assert seqs == [7, 8]
    assert st["oldest_seq"] == 7 and st["newest_seq"] == 8
    # 再多传一片，覆盖最旧的 7
    upload(client, 9)
    st = wait_reclaim(client)
    seqs = sorted(s["seq"] for s in st["slots"] if not s["empty"])
    assert seqs == [8, 9]


def test_ring_overwrite_respects_new_limit_file_count(client, tmp_path):
    for s in range(1, 5):
        upload(client, s)
    resize(client, 2)
    wait_reclaim(client)
    for s in range(5, 9):
        upload(client, s)
    wait_reclaim(client)
    files = sorted(p.name for p in (tmp_path / "data").iterdir())
    assert len(files) == 2
    assert all(name.startswith("slot_") for name in files)


# ---- 调容期间不复制录像 ----


def test_shrink_does_not_copy_or_move_retained_files(client, tmp_path):
    for s in range(1, 9):  # 4 槽环形 -> 留存 5,6,7,8；槽 0..3 代次均为 2
        upload(client, s)
    data_dir = tmp_path / "data"
    before = {
        p.name: (p.stat().st_ino, p.read_bytes()) for p in data_dir.glob("slot_*.seg")
    }
    st_before = client.get("/v1/state").json()
    gen_before = {
        s["slot"]: s["generation"] for s in st_before["slots"] if not s["empty"]
    }

    resize(client, 2)  # 保留最新 7、8
    st = wait_reclaim(client)
    retained = {s["slot"]: s for s in st["slots"] if s["active"] and not s["empty"]}
    assert sorted(s["seq"] for s in retained.values()) == [7, 8]
    for slot, meta in retained.items():
        name = f"slot_{slot:04d}.seg"
        ino, content = before[name]
        assert (data_dir / name).stat().st_ino == ino  # 未复制/未搬动
        assert (data_dir / name).read_bytes() == content
        assert meta["generation"] == gen_before[slot]  # 未重写槽位
    # 仅保留片文件仍在；淘汰片文件删除；没有 trash 残留
    assert sorted(p.name for p in data_dir.iterdir()) == sorted(
        f"slot_{slot:04d}.seg" for slot in retained
    )


# ---- 调大：照常、但不恢复已淘汰片 ----


def test_grow_does_not_restore_evicted(client):
    for s in range(1, 5):
        upload(client, s, independent=(s == 4))
    resize(client, 2)  # 淘汰 1、2
    st = wait_reclaim(client)
    assert [s["slot"] for s in st["slots"] if s["active"]] == [2, 3]

    r = resize(client, 4)
    assert r.status_code == 200
    assert r.json()["activated_slots"] == [0, 1]
    st = wait_reclaim(client)
    seqs = sorted(s["seq"] for s in st["slots"] if not s["empty"])
    assert seqs == [3, 4]  # 1、2 不恢复
    assert st["oldest_seq"] == 3
    # 起播片 1 仍报断档
    assert client.get("/v1/playback/start", params={"seq": 1}).status_code == 410


def test_grow_then_upload_uses_new_capacity(client):
    for s in range(1, 7):
        upload(client, s)
    resize(client, 2)
    wait_reclaim(client)
    resize(client, 4)
    for s in range(7, 11):
        upload(client, s)
    st = wait_reclaim(client)
    seqs = sorted(s["seq"] for s in st["slots"] if not s["empty"])
    assert seqs == [7, 8, 9, 10]


# ---- 回收状态、失败重试、与调小的门禁 ----


class FlakyUnlink:
    """前 fail_times 次 unlink 抛 OSError，之后成功。"""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, path: str):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError("simulated disk error")
        import os

        os.unlink(path)


def test_state_reports_pending_bytes_until_deleted(tmp_path):
    flaky = FlakyUnlink(fail_times=10**9)  # 永不成功
    app, c = make_client(tmp_path, unlink_fn=flaky)
    with c:
        for s in range(1, 5):
            upload(c, s)
        r = resize(c, 2)
        assert r.status_code == 200
        st = c.get("/v1/state").json()
        assert st["pending_delete_bytes"] == 2 * SEG
        assert st["reclaim_complete"] is False
        assert st["reclaim"]["pending_segments"] == 2
        assert st["reclaim"]["failed_segments"] >= 1

        # 回收未完时拒绝再次调小
        r = resize(c, 1)
        assert r.status_code == 409
        body = r.json()
        assert body["status"] == "reclaim-incomplete"
        assert body["pending_delete_bytes"] == 2 * SEG

        # 调大仍允许
        assert resize(c, 4).status_code == 200


def test_failed_reclaim_can_be_retried_and_completes(tmp_path):
    flaky = FlakyUnlink(fail_times=10**9)
    app, c = make_client(tmp_path, unlink_fn=flaky)
    with c:
        for s in range(1, 5):
            upload(c, s)
        resize(c, 2)
        st = c.get("/v1/state").json()
        assert st["reclaim_complete"] is False
        assert st["pending_delete_bytes"] == 2 * SEG

        flaky.fail_times = flaky.calls  # 之后都成功
        r = c.post("/v1/slots/reclaim")
        assert r.status_code == 200
        body = r.json()
        assert body["reclaim"]["complete"] is True
        st = c.get("/v1/state").json()
        assert st["pending_delete_bytes"] == 0
        assert st["reclaim_complete"] is True
        # 回收完成后可再次调小
        assert resize(c, 1).status_code == 200


def test_upload_and_playback_work_during_pending_reclaim(tmp_path):
    flaky = FlakyUnlink(fail_times=10**9)
    app, c = make_client(tmp_path, unlink_fn=flaky)
    with c:
        for s in range(1, 5):
            upload(c, s, independent=(s == 4))
        resize(c, 2)  # 淘汰 1、2，删除一直失败
        st = c.get("/v1/state").json()
        assert st["reclaim_complete"] is False

        # 上传照常
        assert upload(c, 5, independent=True).status_code == 201
        # 回看照常：从片 5 起播读到边缘
        cur = cursor_for(c, 5)
        data, last = read_all(c, cur)
        assert last.status_code == 204 and data == seg_bytes(5)
        # 保留片 4 仍可起播
        assert cursor_for(c, 4)


def test_old_cleanup_never_deletes_reused_slot(tmp_path):
    """回收未完成 -> 调大上限复用淘汰槽位并写入新片 -> 旧清理不能删新片。"""
    flaky = FlakyUnlink(fail_times=10**9)
    app, c = make_client(tmp_path, unlink_fn=flaky)
    with c:
        for s in range(1, 5):
            upload(c, s)
        resize(c, 2)  # 淘汰槽 0(片1)、槽 1(片2)
        st = c.get("/v1/state").json()
        retired_slots = {entry["slot"] for entry in st["retired"]}
        assert retired_slots == {0, 1}

        # 调大上限 -> 复用槽位（优先无待删文件的槽，但两者都可能被用到）
        resize(c, 4)
        upload(c, 5, independent=True)
        upload(c, 6)
        # 现在槽 0/1 中有些被新片占用
        st = c.get("/v1/state").json()
        reuse_slots = {
            s["slot"]
            for s in st["slots"]
            if not s["empty"] and s["seq"] in (5, 6)
        }
        assert reuse_slots

        # 让删除全部成功，触发旧清理
        flaky.fail_times = flaky.calls
        r = c.post("/v1/slots/reclaim")
        assert r.status_code == 200

        # 新片内容完好，仍能续读；数据目录里没有 trash 残留
        for seq in (5, 6):
            slot = next(
                s["slot"]
                for s in c.get("/v1/state").json()["slots"]
                if not s["empty"] and s["seq"] == seq
            )
            p = tmp_path / "data" / f"slot_{slot:04d}.seg"
            assert p.read_bytes() == seg_bytes(seq)
        assert not list((tmp_path / "data").glob("trash_*"))
        # 回看新片正常
        cur = cursor_for(c, 5)
        data, last = read_all(c, cur)
        assert last.status_code == 204
        assert data == seg_bytes(5) + seg_bytes(6)


def test_reclaim_removes_files_from_disk(client, tmp_path):
    for s in range(1, 5):
        upload(client, s)
    resize(client, 2)
    wait_reclaim(client)
    files = sorted(p.name for p in (tmp_path / "data").iterdir())
    assert len(files) == 2
    assert not any(name.startswith("trash_") for name in files)


def test_shrink_reports_retained_and_evicted(client):
    for s in range(1, 7):
        upload(client, s, independent=(s in (4, 6)))
    r = resize(client, 3)
    body = r.json()
    assert body["retained_seqs"] == [4, 5, 6]
    assert body["evicted_seqs"] == [3]
    st = wait_reclaim(client)
    assert st["oldest_seq"] == 4
    # 保留的独立片 4 原游标可续读；能够跨 5 滚到 6
    cur = cursor_for(client, 4)
    data, last = read_all(client, cur)
    assert last.status_code == 204
    assert data == seg_bytes(4) + seg_bytes(5) + seg_bytes(6)


# ---- 边界与并发 ----


def run(coro):
    return asyncio.run(coro)


def test_single_slot_startup(tmp_path):
    app, c = make_client(tmp_path, slot_count=1)
    with c:
        assert upload(c, 1, independent=True, size=1024).status_code == 201
        assert resize(c, 2).status_code == 422  # 不能超过启动槽位数 1
        assert resize(c, 1).status_code == 200
        assert upload(c, 2, size=1024).status_code == 201  # 覆盖片 1
        st = wait_reclaim(c)
        assert st["oldest_seq"] == st["newest_seq"] == 2


def test_resize_to_same_limit_is_noop(client):
    upload(client, 1)
    r = resize(client, SLOT_COUNT)
    assert r.status_code == 200
    assert r.json()["evicted_seqs"] == []
    assert r.json()["retained_seqs"] == [1]


def test_repeated_shrink_grow_cycles_keep_invariants(tmp_path):
    app, c = make_client(tmp_path)
    with c:
        seq = 0
        for cycle in range(4):
            for _ in range(3):
                seq += 1
                assert upload(c, seq, independent=(seq % 3 == 1), size=2048).status_code == 201
            limit = 1 + (cycle % SLOT_COUNT)
            r = resize(c, limit)
            assert r.status_code in (200, 409), r.text
            st = wait_reclaim(c)
            nonempty = [s for s in st["slots"] if not s["empty"]]
            assert len(nonempty) <= st["slot_limit"]
            # 留存片号为最新至多 limit 片
            expected = sorted(range(max(1, seq - limit + 1), seq + 1))
            assert sorted(s["seq"] for s in nonempty) == expected[-len(nonempty):]
            assert st["pending_delete_bytes"] == 0
            assert st["reclaim_complete"]


def test_concurrent_upload_and_shrink(tmp_path):
    """删除进行中，上传/回看/调大并发执行不报错，最终数据只在新上限内。"""

    async def scenario():
        async def flaky(path):
            raise OSError("blocked")

        store = SegmentStore(
            data_dir=str(tmp_path / "data"),
            slot_count=4,
            max_segment_bytes=1 << 20,
            max_read_bytes=1 << 16,
            unlink_fn=flaky,
        )
        for s in range(1, 5):
            await store.put(s, s == 1, bytes([s]) * 4096)

        async def shrink():
            return await store.set_slot_limit(2)

        async def uploads():
            for s in (5, 6):
                await store.put(s, False, bytes([s]) * 4096)

        async def reads():
            cur = await store.start(4)
            if hasattr(cur, "cursor"):
                r = await store.read(cur.cursor.encode(), 1024)
                assert isinstance(r, (ReadOk, Gap))

        async def grow_back():
            return await store.set_slot_limit(4)

        res = await asyncio.gather(
            shrink(), uploads(), reads(), grow_back(), return_exceptions=True
        )
        for item in res:
            assert not isinstance(item, Exception), item
        snap = await store.snapshot()
        # 4 个活动槽位，最多 4 片，且必含最新片 6
        nonempty = [s for s in snap["slots"] if not s["empty"]]
        assert snap["slot_limit"] == 4
        assert len(nonempty) <= 4
        assert max(s["seq"] for s in nonempty) == 6
        await store.aclose()

    run(scenario())
