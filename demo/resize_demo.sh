#!/usr/bin/env bash
# 端到端演示：录像盘吃紧时在线调小槽位上限 -> 淘汰旧片 -> 保留片续读 ->
# 淘汰片报断档 -> 按新上限继续录制 -> 再调大（不恢复已淘汰片）。
#
# 用法：
#   ./demo/resize_demo.sh                 # 用 docker compose 起服务（SLOT_COUNT=4）
#   ./demo/resize_demo.sh --no-compose    # 服务已在运行，直接演（配合 --base）
#   BASE=http://127.0.0.1:8000 ./demo/resize_demo.sh --no-compose
#
# 前提：服务以 SLOT_COUNT=4 运行（compose 文件里已默认）；需要 curl 和 python3。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
COMPOSE=1
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-compose) COMPOSE=0; shift ;;
    --keep)       KEEP=1; shift ;;
    --base)       BASE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s ==\n' "$*"; }

WORK="$(mktemp -d)"
cleanup() {
  rm -rf "$WORK"
  if [[ "$COMPOSE" == 1 && "$KEEP" == 0 ]]; then
    say "收尾: docker compose down"
    (cd "$(dirname "$0")/.." && docker compose down)
  fi
}
trap cleanup EXIT

if [[ "$COMPOSE" == 1 ]]; then
  say "启动服务 (docker compose up -d --build)"
  (cd "$(dirname "$0")/.." && docker compose up -d --build)
fi

say "等待服务就绪 $BASE"
ready=0
for _ in $(seq 1 60); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo "服务未就绪"; exit 1; }
echo "  /healthz OK"

json_field() { python3 -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

# 100 KiB 分片：整片同一字节（值=片号），便于肉眼和脚本核对。
make_seg() { python3 -c "import sys;sys.stdout.buffer.write(bytes([$1 % 256])*102400)" > "$WORK/seg_$1.bin"; }

upload() { # <seq> <independent>
  local code
  code=$(curl -sS -o "$WORK/put_body" -w '%{http_code}' \
    -H 'Content-Type: application/octet-stream' \
    --data-binary @"$WORK/seg_$1.bin" \
    "$BASE/v1/segments/$1?independent=$2")
  echo "  POST /v1/segments/$1?independent=$2 -> $code (slot=$(json_field slot < "$WORK/put_body" 2>/dev/null || echo ?), limit=$(json_field slot_limit < "$WORK/put_body" 2>/dev/null || echo ?))"
}

CURSOR=""; START_SEQ=""; READ_CODE=""; XSEQ=""; XOFF=""
start_playback() { # <seq>
  local code
  code=$(curl -sS -o "$WORK/start_body" -w '%{http_code}' "$BASE/v1/playback/start?seq=$1")
  echo "  GET /v1/playback/start?seq=$1 -> $code $(cat "$WORK/start_body")"
  if [[ "$code" == 200 ]]; then
    CURSOR=$(json_field cursor < "$WORK/start_body")
    START_SEQ=$(json_field start_seq < "$WORK/start_body")
  fi
}

read_next() { # <outfile>  —— 用全局 CURSOR，成功后推进 CURSOR
  local code
  code=$(curl -sS -D "$WORK/headers" -o "$1" -w '%{http_code}' \
    "$BASE/v1/playback/read?cursor=$CURSOR")
  READ_CODE=$code
  XSEQ=""; XOFF=""
  if [[ "$code" == 200 ]]; then
    CURSOR=$(sed -n 's/^[Xx]-[Nn]ext-[Cc]ursor: //p' "$WORK/headers" | tr -d '\r')
    XSEQ=$(sed -n 's/^[Xx]-[Ss]eq: //p' "$WORK/headers" | tr -d '\r')
    XOFF=$(sed -n 's/^[Xx]-[Oo]ffset: //p' "$WORK/headers" | tr -d '\r')
  fi
}

state_json() { curl -sS "$BASE/v1/state"; }

show_state() {
  state_json | python3 -c '
import sys, json
st = json.load(sys.stdin)
parts = []
for s in st["slots"]:
    tag = "" if s["active"] else "x"   # x = 非活动槽位
    if s["empty"]:
        parts.append("%s%d:-" % (tag, s["slot"]))
    else:
        mark = "I" if s["independent"] else " "
        parts.append("%s%d:seq%d%s(g%d)" % (tag, s["slot"], s["seq"], mark, s["generation"]))
print("  limit=%s active=%s" % (st["slot_limit"], st["active_slots"]))
print("  slots: " + ", ".join(parts))
print("  oldest=%s newest=%s earliest_playable=%s" % (st["oldest_seq"], st["newest_seq"], st["earliest_playable"]))
print("  pending_delete_bytes=%s reclaim_complete=%s" % (st["pending_delete_bytes"], st["reclaim_complete"]))'
}

wait_reclaim() {
  state_json | python3 -c '
import sys, json
print(json.load(sys.stdin)["reclaim_complete"])' | grep -q True
}

set_limit() { # <n>
  local code
  code=$(curl -sS -o "$WORK/limit_body" -w '%{http_code}' -X PUT "$BASE/v1/slots/limit?limit=$1")
  echo "  PUT /v1/slots/limit?limit=$1 -> $code"
  if [[ "$code" == 200 ]]; then
    python3 - "$WORK/limit_body" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
print("  previous_limit=%s slot_limit=%s retained=%s evicted=%s activated=%s"
      % (b["previous_limit"], b["slot_limit"], b["retained_seqs"],
         b["evicted_seqs"], b["activated_slots"]))
print("  待删旧片占用 evicted_delete_bytes=%s，当前 pending_delete_bytes=%s，reclaim_complete=%s"
      % (b["evicted_delete_bytes"], b["pending_delete_bytes"], b["reclaim_complete"]))
PY
  else
    cat "$WORK/limit_body"; echo
  fi
}

say "0. 初始状态"
show_state

say "1. 上传片 1..4 写满 4 槽（片 1、4 可独立解码）"
for s in 1 2 3 4; do make_seg "$s"; done
upload 1 true; upload 2 false; upload 3 false; upload 4 true
show_state

say "2. 两个终端分别从片 1、片 4 起播，各读 64 KiB，留下原游标"
start_playback 1
CURSOR_SEG1="$CURSOR"
read_next "$WORK/p1_c1" >/dev/null
CURSOR_MID_SEG1="$CURSOR"
echo "  片 1 游标停在 offset=$XOFF"

start_playback 4
CURSOR_SEG4="$CURSOR"
read_next "$WORK/p4_c1" >/dev/null
CURSOR_MID_SEG4="$CURSOR"
echo "  片 4 游标停在 offset=$XOFF"

say "3. 录像盘吃紧：在线把槽位上限从 4 调到 2（保留生效时最新的 2 片：片 3、4）"
set_limit 2
echo "  （状态接口报告待删旧片字节；删除完成后才报回收完成）"
for _ in $(seq 1 100); do wait_reclaim && break; sleep 0.05; done
show_state

say "4. 保留片 4 用原游标从原偏移续读（不复制、不搬动录像）"
CURSOR="$CURSOR_MID_SEG4"
read_next "$WORK/p4_c2"
echo "  read -> $READ_CODE 片$XSEQ offset=$XOFF $(stat -c%s "$WORK/p4_c2") 字节"
tail -c 36864 "$WORK/seg_4.bin" > "$WORK/exp_p4_tail"
cmp "$WORK/exp_p4_tail" "$WORK/p4_c2" && echo "  OK: 保留片续读字节与上传完全一致"

say "5. 淘汰片 1 的旧游标续读 -> 410 断档（不会读到复用槽位的其它片）"
CURSOR="$CURSOR_MID_SEG1"
read_next "$WORK/gap1"
echo "  read -> $READ_CODE $(cat "$WORK/gap1")"

say "6. 缩容后继续录制：上传片 5、6，按新上限 2 覆盖最旧的片 3、4"
make_seg 5; make_seg 6
upload 5 true; upload 6 false
show_state

say "7. 起播并读到直播边缘（片 5 为独立片，片 6 续在其后）"
start_playback 6
i=0
while true; do
  i=$((i+1))
  read_next "$WORK/new_r$i"
  echo "  read#$i -> $READ_CODE 片${XSEQ:-?} offset=${XOFF:-?} $(stat -c%s "$WORK/new_r$i") 字节"
  [[ "$READ_CODE" == 200 ]] || break
  [[ $i -lt 20 ]] || { echo "  异常：读的次数过多"; exit 1; }
done
cat "$WORK"/new_r* > "$WORK/got_new"
cat "$WORK/seg_5.bin" "$WORK/seg_6.bin" > "$WORK/exp_new"
cmp "$WORK/exp_new" "$WORK/got_new" && echo "  OK: 缩容后新录的片 5+6 读回字节与上传完全一致"

say "8. 空间缓解后调回上限 4：只恢复容量，不恢复已淘汰的片"
set_limit 4
for _ in $(seq 1 100); do wait_reclaim && break; sleep 0.05; done
show_state
start_playback 1
echo "  （片 1 已淘汰，仍报 410 而不是复活）"

say "9. 状态与磁盘文件"
if [[ "$COMPOSE" == 1 ]]; then
  echo "  容器内 /data:"
  (cd "$(dirname "$0")/.." && docker compose exec -T recorder ls -1 /data | sed 's/^/    /')
else
  show_state
fi

say "演示完成"
