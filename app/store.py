"""录像分片环形存储（支持在线调容）。

模型：
- 启动配置 slot_count 个物理槽位（每个槽位对应磁盘上一个文件），运行期有一个
  1..slot_count 的"槽位上限" limit，只有 limit 个物理槽位处于活动状态。
- 编码器按连续片号上传；活动槽位写满后，新片覆盖活动槽位中最旧片。
- 每次覆盖使槽位代次(generation) +1。游标里带着 (slot, generation, seq, offset)，
  旧游标对不上当前槽位元数据即判为断档，绝不会读到同槽新片的字节。
- 调小上限时，在锁内选出"生效时最新的至多 N 片"保留（槽位、代次、文件都不动，
  因此原游标可原偏移续读），其余片标为淘汰并排队删文件；淘汰片的游标报断档。
- 调大上限只是把非活动槽位重新纳入活动集合，不会恢复任何已淘汰的片。
- 删除淘汰片时先把旧文件原子改名到 trash 路径，再在锁外 unlink。这样即使该
  物理槽位被调大上限复用并写入新片，清理任务也只会删 trash，不会删错新片。
- 元数据全在内存，由一把 asyncio.Lock 保护；文件读写经 to_thread 挂到锁内，
  因此"校验游标 -> 读字节"与"覆盖写文件 -> 改元数据"互斥，不会读到半新半旧。

读路径只在单个请求内短暂持锁（最多读 64 KiB），请求之间不持有任何资源；
写路径不等待任何读者。所以慢终端既拖不住写入，也不会多占磁盘槽。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union


class StoreError(Exception):
    """存储层错误基类。"""


class NonContiguous(StoreError):
    """片号不连续（不是 last_seq + 1）。"""

    def __init__(self, expected: int, got: int):
        super().__init__(f"expected seq {expected}, got {got}")
        self.expected = expected
        self.got = got


class EmptySegment(StoreError):
    """空分片体。"""


class SegmentTooLarge(StoreError):
    """超过单片上限。"""


class BadCursor(StoreError):
    """游标无法解析或自相矛盾。"""


class InvalidSlotLimit(StoreError):
    """槽位上限不在 1..启动配置的槽位数 之间。"""

    def __init__(self, limit: int, configured: int):
        super().__init__(f"slot limit {limit} out of range 1..{configured}")
        self.limit = limit
        self.configured = configured


class ReclaimIncomplete(StoreError):
    """上一轮缩容淘汰的旧片尚未删完，拒绝再次调小。"""

    def __init__(self, pending_bytes: int, pending_segments: int):
        super().__init__(
            "reclaim of evicted segments is not complete "
            f"({pending_segments} file(s), {pending_bytes} bytes)"
        )
        self.pending_bytes = pending_bytes
        self.pending_segments = pending_segments


@dataclass
class SlotMeta:
    seq: int
    independent: bool
    generation: int
    size: int


@dataclass
class RetiredFile:
    """一片已淘汰、等待删除的旧文件。

    path 可能是槽位原路径（尚未搬走），也可能是 data_dir 下的 trash 路径
    （已原子改名、等待 unlink）。trash 路径与槽位路径互不相干，删除它不会
    影响复用该槽位后写入的新片。
    """

    slot: int
    generation: int
    seq: int
    size: int
    path: Path
    state: str = "pending"  # pending(原槽位路径) / moved(trash) / unlinking
    attempts: int = 0
    last_error: Optional[str] = None


@dataclass
class Cursor:
    """续读游标：定位到某槽某代某片的某字节偏移。"""

    slot: int
    generation: int
    seq: int
    offset: int

    def encode(self) -> str:
        payload = json.dumps(
            {"s": self.slot, "g": self.generation, "q": self.seq, "o": self.offset},
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(payload).decode()

    @classmethod
    def decode(cls, token: str) -> "Cursor":
        try:
            raw = base64.urlsafe_b64decode(token.encode())
            obj = json.loads(raw)
            cur = cls(
                slot=int(obj["s"]),
                generation=int(obj["g"]),
                seq=int(obj["q"]),
                offset=int(obj["o"]),
            )
        except (binascii.Error, ValueError, KeyError, TypeError, UnicodeEncodeError) as exc:
            raise BadCursor(f"invalid cursor: {exc}") from exc
        if cur.slot < 0 or cur.generation < 0 or cur.seq < 0 or cur.offset < 0:
            raise BadCursor("cursor fields must be non-negative")
        return cur


# ---- 操作结果 ----


@dataclass
class PutResult:
    seq: int
    slot: int
    generation: int
    size: int
    independent: bool
    oldest_seq: int
    newest_seq: int
    slot_limit: int


@dataclass
class StartOk:
    start_seq: int
    cursor: Cursor


@dataclass
class ReadOk:
    data: bytes
    seq: int
    offset: int
    segment_size: int
    independent: bool
    next_cursor: Cursor


@dataclass
class Gap:
    """断档：所要的片已被覆盖/淘汰。给出最早仍可起播的片号（可能为 None）。"""

    earliest_playable: Optional[int]


@dataclass
class Wait:
    """等待：暂无可用起点，或下一片尚未写入。"""

    reason: str


@dataclass
class ResizeResult:
    previous_limit: int
    slot_limit: int
    retained_seqs: list[int]
    evicted_seqs: list[int]
    activated_slots: list[int]
    pending_delete_bytes: int


StartResult = Union[StartOk, Gap, Wait]
ReadResult = Union[ReadOk, Gap, Wait]


class SegmentStore:
    def __init__(
        self,
        data_dir: str,
        slot_count: int,
        max_segment_bytes: int,
        max_read_bytes: int,
        unlink_fn: Optional[Callable[[str], None]] = None,
    ):
        if slot_count < 1:
            raise ValueError("slot_count must be >= 1")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.slot_count = slot_count
        self.max_segment_bytes = max_segment_bytes
        self.max_read_bytes = max_read_bytes
        self._slots: list[Optional[SlotMeta]] = [None] * slot_count
        self._generations: list[int] = [0] * slot_count
        self._index: dict[int, int] = {}  # seq -> slot（仅活动且留存的片）
        self._last_seq: Optional[int] = None
        # 活动槽位集合与上限；启动时等于配置的物理槽位数。
        self._active: list[int] = list(range(slot_count))
        self._active_set: set[int] = set(self._active)
        self._limit = slot_count
        # (slot, generation) -> 待删旧片。generation 每槽单调递增，键不会重复。
        self._retired: dict[tuple[int, int], RetiredFile] = {}
        self._lock = asyncio.Lock()
        self._reclaim_pass_lock = asyncio.Lock()
        self._reclaim_task: Optional[asyncio.Task] = None
        # 便于测试注入删除失败；生产默认 os.unlink。
        self._unlink: Callable[[str], None] = unlink_fn or os.unlink
        self._wipe_dir()

    # ---- 文件层（同步，经 to_thread 调用） ----

    def _wipe_dir(self) -> None:
        # 元数据在内存，重启后无法恢复索引，落盘文件一并清空，从头开始。
        for p in self.data_dir.iterdir():
            if p.is_file():
                p.unlink()

    def _slot_path(self, slot: int) -> Path:
        return self.data_dir / f"slot_{slot:04d}.seg"

    def _tmp_path(self, slot: int, generation: int) -> Path:
        return self.data_dir / f"slot_{slot:04d}.g{generation:08d}.tmp"

    def _trash_path(self, entry: RetiredFile) -> Path:
        return self.data_dir / f"trash_s{entry.slot:04d}_g{entry.generation:08d}.del"

    def _write_slot_file(self, slot: int, generation: int, data: bytes) -> None:
        tmp = self._tmp_path(slot, generation)
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._slot_path(slot))

    def _read_slot_file(self, slot: int, offset: int, length: int) -> bytes:
        with open(self._slot_path(slot), "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    # ---- 元数据辅助（须在锁内调用） ----

    def _earliest_playable_locked(self) -> Optional[int]:
        seqs = [m.seq for m in self._slots if m is not None and m.independent]
        return min(seqs) if seqs else None

    def _oldest_seq_locked(self) -> Optional[int]:
        seqs = [m.seq for m in self._slots if m is not None]
        return min(seqs) if seqs else None

    def _pick_slot_locked(self) -> int:
        for slot in self._active:
            if self._slots[slot] is None:
                return slot
        # 活动槽位写满了：覆盖活动槽位中最旧片。
        return min(self._active, key=lambda i: self._slots[i].seq)

    def _set_active_locked(self, slots: list[int]) -> None:
        self._active = sorted(slots)
        self._active_set = set(self._active)
        self._limit = len(self._active)

    def _pending_bytes_locked(self) -> int:
        return sum(entry.size for entry in self._retired.values())

    def _slot_has_pending_locked(self, slot: int) -> bool:
        slot_path = self._slot_path(slot)
        return any(
            entry.slot == slot and entry.path == slot_path
            for entry in self._retired.values()
        )

    def _retire_slot_locked(self, slot: int) -> Optional[RetiredFile]:
        """把槽位上的片标为淘汰：清索引/元数据，文件留给回收任务删除。"""
        meta = self._slots[slot]
        if meta is None:
            return None
        self._index.pop(meta.seq, None)
        self._slots[slot] = None
        entry = RetiredFile(
            slot=slot,
            generation=meta.generation,
            seq=meta.seq,
            size=meta.size,
            path=self._slot_path(slot),
        )
        self._retired[(slot, meta.generation)] = entry
        return entry

    def _discard_replaced_slot_path_entry_locked(self, slot: int) -> None:
        """新片写入后，原槽位路径上的待删旧文件已被原子替换，不再需要删除。"""
        slot_path = self._slot_path(slot)
        for key, entry in list(self._retired.items()):
            if entry.slot == slot and entry.path == slot_path:
                del self._retired[key]

    # ---- 对外操作 ----

    async def put(self, seq: int, independent: bool, data: bytes) -> PutResult:
        if not data:
            raise EmptySegment("empty segment body")
        if len(data) > self.max_segment_bytes:
            raise SegmentTooLarge(
                f"segment is {len(data)} bytes, limit is {self.max_segment_bytes}"
            )
        async with self._lock:
            if self._last_seq is not None and seq != self._last_seq + 1:
                raise NonContiguous(expected=self._last_seq + 1, got=seq)
            slot = self._pick_slot_locked()
            self._generations[slot] += 1
            generation = self._generations[slot]
            await asyncio.to_thread(self._write_slot_file, slot, generation, data)
            old = self._slots[slot]
            if old is not None:
                self._index.pop(old.seq, None)
            self._slots[slot] = SlotMeta(
                seq=seq, independent=independent, generation=generation, size=len(data)
            )
            self._index[seq] = slot
            # 若该槽位原先有等待删除的旧文件，新片已经原子覆盖了它。
            self._discard_replaced_slot_path_entry_locked(slot)
            self._last_seq = seq
            return PutResult(
                seq=seq,
                slot=slot,
                generation=generation,
                size=len(data),
                independent=independent,
                oldest_seq=self._oldest_seq_locked(),
                newest_seq=self._last_seq,
                slot_limit=self._limit,
            )

    async def start(self, seq: int) -> StartResult:
        """从"不晚于 seq、仍留存的最近独立片"起播。"""
        async with self._lock:
            best: Optional[int] = None
            for meta in self._slots:
                if meta is None or not meta.independent or meta.seq > seq:
                    continue
                if best is None or meta.seq > best:
                    best = meta.seq
            if best is not None:
                slot = self._index[best]
                meta = self._slots[slot]
                return StartOk(
                    start_seq=best,
                    cursor=Cursor(slot=slot, generation=meta.generation, seq=best, offset=0),
                )
            earliest = self._earliest_playable_locked()
            if earliest is not None:
                # 还有可起播的片，但都比所求片号新 -> 所求起点已被覆盖/淘汰。
                return Gap(earliest_playable=earliest)
            # 库里一片可独立解码的都没有 -> 等编码器继续写。
            return Wait(reason="no-independent-start")

    async def read(self, token: str, max_bytes: int) -> ReadResult:
        cursor = Cursor.decode(token)
        # 用启动配置的物理槽位数做合法性检查；被淘汰/复用的槽位交给下面的
        # 元数据比对，报 410 断档而不是 400。
        if cursor.slot >= self.slot_count:
            raise BadCursor("cursor slot out of range")
        max_bytes = max(1, min(max_bytes, self.max_read_bytes))
        async with self._lock:
            slot = cursor.slot
            generation = cursor.generation
            seq = cursor.seq
            offset = cursor.offset
            while True:
                meta = self._slots[slot]
                if meta is None or meta.seq != seq or meta.generation != generation:
                    # 槽位被覆盖/淘汰（或重启清空）：旧游标读不到同槽新片。
                    return Gap(earliest_playable=self._earliest_playable_locked())
                if offset > meta.size:
                    raise BadCursor("cursor offset beyond segment size")
                if offset < meta.size:
                    length = min(max_bytes, meta.size - offset)
                    data = await asyncio.to_thread(
                        self._read_slot_file, slot, offset, length
                    )
                    return ReadOk(
                        data=data,
                        seq=seq,
                        offset=offset,
                        segment_size=meta.size,
                        independent=meta.independent,
                        next_cursor=Cursor(
                            slot=slot,
                            generation=generation,
                            seq=seq,
                            offset=offset + length,
                        ),
                    )
                # 本片已读完，顺势滚到下一片。
                next_slot = self._index.get(seq + 1)
                if next_slot is None:
                    if self._last_seq is not None and seq + 1 <= self._last_seq:
                        # 片号曾写入过却不在库里了 -> 被覆盖/淘汰。
                        return Gap(earliest_playable=self._earliest_playable_locked())
                    return Wait(reason="next-segment-not-written")
                slot = next_slot
                seq = seq + 1
                offset = 0
                generation = self._slots[slot].generation

    # ---- 在线调容 ----

    async def set_slot_limit(self, limit: int) -> ResizeResult:
        """在线设置活动槽位上限（1..slot_count）。

        调小：保留生效时最新的至多 limit 片；其余片清出索引并排队删文件。
        调大：把非活动槽位纳入活动集合；已淘汰的片不会恢复。
        回收未完成时拒绝再次调小（调大/不变仍允许）。
        """
        if limit < 1 or limit > self.slot_count:
            raise InvalidSlotLimit(limit, self.slot_count)
        async with self._lock:
            previous = self._limit
            if limit == previous:
                return ResizeResult(
                    previous_limit=previous,
                    slot_limit=self._limit,
                    retained_seqs=sorted(
                        m.seq for m in self._slots if m is not None
                    ),
                    evicted_seqs=[],
                    activated_slots=[],
                    pending_delete_bytes=self._pending_bytes_locked(),
                )

            if limit < previous:
                if self._retired:
                    raise ReclaimIncomplete(
                        pending_bytes=self._pending_bytes_locked(),
                        pending_segments=len(self._retired),
                    )
                # 生效时活动槽位里最新的至多 limit 片保留。
                nonempty = sorted(
                    (
                        (self._slots[slot].seq, slot)
                        for slot in self._active
                        if self._slots[slot] is not None
                    ),
                    reverse=True,
                )
                keep = {slot for _, slot in nonempty[:limit]}
                need = limit - len(keep)
                if need > 0:
                    # 片数不足上限时，用活动集合里的空槽补足容量；不搬动任何文件。
                    empties = [
                        slot for slot in self._active if self._slots[slot] is None
                    ]
                    keep.update(empties[:need])
                new_active = sorted(keep)
                evicted = [
                    slot
                    for slot in self._active
                    if slot not in keep and self._slots[slot] is not None
                ]
                evicted_seqs = sorted(self._slots[slot].seq for slot in evicted)
                for slot in sorted(evicted, key=lambda s: self._slots[s].seq):
                    self._retire_slot_locked(slot)
                retained_seqs = sorted(
                    self._slots[slot].seq
                    for slot in new_active
                    if self._slots[slot] is not None
                )
                self._set_active_locked(new_active)
                self._ensure_reclaim_task_locked()
                return ResizeResult(
                    previous_limit=previous,
                    slot_limit=self._limit,
                    retained_seqs=retained_seqs,
                    evicted_seqs=evicted_seqs,
                    activated_slots=[],
                    pending_delete_bytes=self._pending_bytes_locked(),
                )

            # limit > previous：只扩充容量，不恢复任何已淘汰的片。
            need = limit - previous
            candidates = [
                slot for slot in range(self.slot_count) if slot not in self._active_set
            ]
            # 优先用没有待删旧文件的槽位，避免不必要的搬动；语义上两者都安全。
            candidates.sort(key=lambda s: (self._slot_has_pending_locked(s), s))
            chosen = candidates[:need]
            if len(chosen) < need:
                # 正常情况下不会发生：非活动槽位数 == slot_count - previous。
                raise InvalidSlotLimit(limit, self.slot_count)
            self._set_active_locked(sorted(self._active_set | set(chosen)))
            if self._retired:
                self._ensure_reclaim_task_locked()
            return ResizeResult(
                previous_limit=previous,
                slot_limit=self._limit,
                retained_seqs=sorted(
                    m.seq for m in self._slots if m is not None
                ),
                evicted_seqs=[],
                activated_slots=sorted(chosen),
                pending_delete_bytes=self._pending_bytes_locked(),
            )

    # ---- 淘汰文件回收 ----

    def _ensure_reclaim_task_locked(self) -> None:
        if not self._retired:
            return
        if self._reclaim_task is None or self._reclaim_task.done():
            self._reclaim_task = asyncio.create_task(self._reclaim_loop())

    async def _reclaim_loop(self) -> None:
        """后台回收：每轮尝试删一轮；失败按指数退避重试，直到删完。"""
        backoff = 0.05
        try:
            while True:
                async with self._lock:
                    if not self._retired:
                        return
                await self._run_reclaim_pass()
                async with self._lock:
                    if not self._retired:
                        return
                    failed = any(e.last_error for e in self._retired.values())
                if failed:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 2.0)
                else:
                    await asyncio.sleep(0)
        finally:
            if self._reclaim_task is asyncio.current_task():
                self._reclaim_task = None

    async def _run_reclaim_pass(self) -> None:
        """对当前待删快照各尝试一次：先在锁内原子搬进 trash，再在锁外 unlink。

        "搬进 trash"必须在锁内完成：若在锁外做，可能与上传的原子替换交错，
        把复用该槽位后写入的新片搬走。改成 trash 名带 (slot, generation)，
        随后在锁外 unlink 的那个文件必定是旧片，不会碰到槽位路径。
        """
        async with self._reclaim_pass_lock:
            attempted: set[tuple[int, int]] = set()
            while True:
                async with self._lock:
                    entry: Optional[RetiredFile] = None
                    key: Optional[tuple[int, int]] = None
                    for candidate_key, candidate in self._retired.items():
                        if candidate_key not in attempted:
                            entry = candidate
                            key = candidate_key
                            break
                    if entry is None or key is None:
                        return
                    slot_path = self._slot_path(entry.slot)
                    if entry.path == slot_path:
                        if self._slots[entry.slot] is not None:
                            # 该槽位已被复用并写入了新片，旧文件已被原子替换。
                            del self._retired[key]
                            continue
                        trash = self._trash_path(entry)
                        try:
                            os.replace(slot_path, trash)
                        except FileNotFoundError:
                            del self._retired[key]
                            continue
                        except OSError as exc:
                            entry.attempts += 1
                            entry.last_error = f"move to trash failed: {exc}"
                            attempted.add(key)
                            continue
                        entry.path = trash
                        entry.state = "moved"
                    entry.state = "unlinking"
                    path = str(entry.path)
                try:
                    await asyncio.to_thread(self._unlink, path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    async with self._lock:
                        current = self._retired.get(key)
                        if current is not None:
                            current.state = "moved"
                            current.attempts += 1
                            current.last_error = f"unlink failed: {exc}"
                    attempted.add(key)
                    continue
                async with self._lock:
                    self._retired.pop(key, None)

    async def retry_reclaim(self) -> dict:
        """立即重试删除失败的旧片，返回状态快照。"""
        async with self._lock:
            has_pending = bool(self._retired)
        if has_pending:
            await self._run_reclaim_pass()
            async with self._lock:
                if self._retired:
                    self._ensure_reclaim_task_locked()
        return await self.snapshot()

    async def aclose(self) -> None:
        task = self._reclaim_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reclaim_task = None

    # ---- 状态 ----

    async def snapshot(self) -> dict:
        async with self._lock:
            pending = list(self._retired.values())
            pending_bytes = sum(entry.size for entry in pending)
            failed = [entry for entry in pending if entry.last_error]
            slots = []
            for i in range(self.slot_count):
                meta = self._slots[i]
                if meta is not None:
                    slots.append(
                        {
                            "slot": i,
                            "active": i in self._active_set,
                            "empty": False,
                            "seq": meta.seq,
                            "independent": meta.independent,
                            "generation": meta.generation,
                            "size": meta.size,
                        }
                    )
                else:
                    item = {
                        "slot": i,
                        "active": i in self._active_set,
                        "empty": True,
                    }
                    slots.append(item)
            return {
                "slot_count": self.slot_count,
                "slot_limit": self._limit,
                "active_slots": list(self._active),
                "max_segment_bytes": self.max_segment_bytes,
                "max_read_bytes": self.max_read_bytes,
                "newest_seq": self._last_seq,
                "oldest_seq": self._oldest_seq_locked(),
                "earliest_playable": self._earliest_playable_locked(),
                "pending_delete_bytes": pending_bytes,
                "reclaim_complete": not pending,
                "retired": [
                    {
                        "slot": entry.slot,
                        "seq": entry.seq,
                        "generation": entry.generation,
                        "size": entry.size,
                        "state": entry.state,
                        "attempts": entry.attempts,
                        "last_error": entry.last_error,
                    }
                    for entry in pending
                ],
                "reclaim": {
                    "pending_bytes": pending_bytes,
                    "pending_segments": len(pending),
                    "complete": not pending,
                    "in_progress": self._reclaim_task is not None
                    and not self._reclaim_task.done(),
                    "failed_segments": len(failed),
                    "last_error": failed[-1].last_error if failed else None,
                },
                "slots": slots,
            }
