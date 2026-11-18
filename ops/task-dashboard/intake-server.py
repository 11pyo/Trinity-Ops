# -*- coding: utf-8 -*-
"""
intake-server.py — SD 문의 접수 서버 (사내망 공개용)

역할
  · 현업이 브라우저로 SD 문의를 직접 접수한다.
  · 접수한 본인이 「내 문의함」에서 자기 문의와 답변만 확인한다.

⛔ 설계 원칙 — 이 파일을 고칠 때 반드시 지킨다
  1. 쓰기는 **「본인 문의에 대한 입력」** 뿐이다 — ①신규 접수 ②본인 문의에 추가 질문 ③본인 문의 종결 표시([케이스 닫기]).
     그 셋 말고는 만들지 말 것. **내용·답변·유형을 고치는 엔드포인트, 삭제 엔드포인트는 없다.**
     ③ 은 상태 「완료」와 req_closed(닫은 시각)만 기록하고, 닫은 뒤 추가 질문이 붙으면 다시 열린다
     (2026-10-06 사용자 승인 — 문의자는 답을 받고 케이스를 닫는다. 담당자는 복귀 보고서로 답변 판정을 이어간다).
     진행 관리는 담당자가 쥐고 있어야 한다.
     (2026-09-10 확장: 원래는 「신규 접수 하나뿐」이었다. 현업이 답변을 보고 더 물으려면
      맥락 없는 새 문의를 넣어야 했다. 추가 질문은 **본인이 자기 문의에 덧붙이는 입력**이라
      원칙의 취지를 깨지 않는다 — 남의 카드도, 상태도, 답변도 건드리지 못한다.)
     (2026-09-10: 스크린샷은 이 두 입력에 **함께 실려서만** 들어온다. 따로 올리는 엔드포인트를
      두지 않는다 — 글은 들어갔는데 그림만 빠지거나, 글 속 [[사진:N]] 번호가 어긋나는 일을 막는다.)
     (담당자 세션·부재중 에이전트는 로컬에서 log-inquiry.py 를 직접 부른다.
      그건 이 서버를 거치지 않으므로 이 원칙과 충돌하지 않는다.)
  2. inquiry-log.js 를 직접 쓰지 않는다. 반드시 log-inquiry.py 를 호출한다
     (append-only · 파일잠금 · 백업 · id 병합이 전부 거기에 있다).
  3. board-saver.py(127.0.0.1 전용 쓰기 서버)와 절대 합치지 말 것. 별개 프로세스다.
  4. ⛔ **담당자 대시보드(task-board.html)를 서빙하지 않는다.** 2026-09-10 이전에는
     /board 로 조회 전용 서빙했으나, 거기엔 DEV·OPS 카드의 🤖 AI 참조 로그(내부 판단·
     소스 분석·실패 경위)가 그대로 들어 있다. 공개 화면은 이 파일이 직접 그리는
     「내 문의함」 하나뿐이다. inquiry-log.js 원본도 서빙하지 않는다(전원 문의가 다 들어있다).
  5. ⛔ **공개 응답은 화이트리스트로만 만든다.** _public_view() 를 거치지 않은 dict 를
     그대로 내보내지 말 것. 필드가 늘어나도 공개본은 자동으로 늘지 않아야 한다.
  6. ⛔ **본인 건만 보인다.** 노출 대상은 by="웹접수" 인 기록뿐이다. 담당자가 메신저·
     ITSM로 받아 적은 기록에는 내부 메모가 섞여 있을 수 있으므로 공개하지 않는다.

내부 메모 규약 (담당자·에이전트가 답변란에 문맥을 적어둘 때)
    [[내부]] 이 줄은 현업에게 보이지 않는다
    [[내부]] … [[/내부]]      ← 문장 중간을 가릴 때
  두 형태 모두 공개 응답에서 제거된다. 대시보드에는 그대로 남는다.

포트 5179 (board-saver 5178 과 구분). 사내망 공개이므로 0.0.0.0 바인딩한다.
"""

import base64
import hashlib
import io
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_JS = os.path.join(HERE, "inquiry-log.js")
HELPER = os.path.join(HERE, "log-inquiry.py")

HOST, PORT = "0.0.0.0", 5179

# HTTPS(2026-10-30) — 브라우저는 http://IP 주소(보안 컨텍스트 아님)에서 팝업 알림을 막는다. 사내 인증서를 받으면 별도 포트로 함께 연다.
# ⛔ 개인 키는 공유 폴더(G:)에 두지 않는다 — 기본 위치는 이 PC 로컬 ~/.trinity-intake-tls (환경변수로 바꿀 수 있다).
# 인증서·키 파일이 없으면 HTTPS 는 열지 않고 기존 http 만 동작한다(기존 동작 그대로).
TLS_PORT = int(os.environ.get("INTAKE_TLS_PORT", "5180"))
TLS_DIR = os.environ.get("INTAKE_TLS_DIR", os.path.join(os.path.expanduser("~"), ".trinity-intake-tls"))
TLS_CERT = os.environ.get("INTAKE_TLS_CERT") or os.path.join(TLS_DIR, "server.crt")   # 서버 인증서(+중간 인증서 이어붙인 PEM)
TLS_KEY = os.environ.get("INTAKE_TLS_KEY") or os.path.join(TLS_DIR, "server.key")

TYPES = ("단순문의", "일반요청", "긴급문의")
WEB_BY = "웹접수"          # 공개 노출 대상 표식 — 이 값이 아닌 기록은 공개하지 않는다
MAX_Q = 2000
MAX_NAME = 40
MAX_ORG = 60
MAX_ROLE = 40             # 담당 업무(2026-09-22~) — 트리니티봇이 담당에 맞춰 답한다. 「내 문의함」 본인 확인에는 쓰지 않는다
RATE_MAX = 5              # 접수: IP 당
RATE_WINDOW = 60          # 초
LOOKUP_MAX = 40           # 조회: IP 당 (스캔 방지)
PULSE_MAX = 60            # 새 답변 확인(/api/pulse): IP 당 — 5초 주기 폴링 + 탭 여러 개여도 넉넉하다
MAX_FOLLOWUPS = 10        # 한 문의에 붙일 수 있는 추가 질문 수

# 스크린샷 첨부 — **공유본 밖**에 둔다. 화면 캡처에는 고객명·금액이 그대로 찍혀 있다.
SHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        ".runtime", "_intake-attachments")
SHOT_MAX_BYTES = 4 * 1024 * 1024      # 장당 4MB
SHOT_MAX_COUNT = 10                   # 문의당 10장 (추가 질문까지 합쳐서)
SHOT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "gif": "image/gif", "webp": "image/webp"}
SHOT_PURGE_SEC = 600                  # 10분마다 완료된 문의의 첨부를 지운다
SHOT_GRACE_SEC = 1800                 # 막 들어온 그림은 30분간 지우지 않는다(추가 질문으로 다시 열리는 중일 수 있다)
IMG_MAX = 300                         # 그림 내려받기: IP 당 분당 (한 화면에 그림이 여러 장 뜬다)
BODY_MAX = SHOT_MAX_BYTES * SHOT_MAX_COUNT * 4 // 3 + 256 * 1024   # 글 + 그림(base64) 한 요청 상한
TOKEN = re.compile(r"\[\[사진:(\d+)\]\]")   # 글 속 그림 자리 표시

# 대화 이어가기 — 문의(q)·답변(a) 안에 이 표시로 턴을 구분한다.
# 파일 구조를 바꾸지 않고 대화를 담기 위한 것이다. 사람이 읽어도 그대로 읽힌다.
Q_MARK = "──── 추가 질문"
A_MARK = "──── 추가 답변"
TURN_RE = re.compile(r"^──── (추가 질문|추가 답변) \(([^)]*)\) ────$")

# 내부 메모 — 공개 응답에서 제거한다 (대시보드에는 그대로 남는다)
MARK_OPEN = "[[내부]]"
MARK_CLOSE = "[[/내부]]"
_INTERNAL_INLINE = re.compile(re.escape(MARK_OPEN) + r".*?" + re.escape(MARK_CLOSE))

_rate = defaultdict(list)
_rate_lock = threading.Lock()
# 등록·추가 질문·그림 정리를 한 줄로 세운다 — 같은 문의에 동시에 두 요청이 오면 그림 번호가 겹치고,
# 정리가 막 쓰는 그림을 지울 수 있다(2026-09-10 리뷰 #6). 서버는 한 프로세스라 이 잠금 하나로 충분하다.
_write_lock = threading.Lock()


def _rate_ok(ip, bucket, limit):
    now = time.time()
    key = "%s|%s" % (ip, bucket)
    with _rate_lock:
        hits = [t for t in _rate[key] if now - t < RATE_WINDOW]
        if len(hits) >= limit:
            _rate[key] = hits
            return False
        hits.append(now)
        _rate[key] = hits
        return True


def _strip_internal(text):
    """답변·문의 본문에서 내부 메모를 걷어낸다.

      · 한 줄 안에서 [[내부]]…[[/내부]] 로 닫힌 구간은 그 구간만 지운다.
      · 닫지 않은 [[내부]] 는 그 줄 끝까지를 내부로 본다.
      · 마커 때문에 통째로 비게 된 줄은 버린다(빈 줄이 남지 않게).

    ⚠️ 여러 줄에 걸친 매칭은 하지 않는다. 닫는 표시를 빠뜨렸을 때 비탐욕 매칭이
       다음 줄의 [[/내부]] 까지 건너뛰며 그 사이의 정상 문장을 통째로 삼킨다
       (2026-09-10 E2E 에서 실제로 발생 — 줄 단위 처리로 바꾼 이유다).
    """
    if not text:
        return ""
    kept = []
    for raw in text.splitlines():
        line = _INTERNAL_INLINE.sub("", raw)
        cut = line.find(MARK_OPEN)
        if cut >= 0:
            line = line[:cut]
        line = line.replace(MARK_CLOSE, "")      # 짝 없는 닫는 표시도 지운다
        if (MARK_OPEN in raw or MARK_CLOSE in raw) and not line.strip():
            continue                              # 마커만 있던 줄
        kept.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _norm(s):
    """이름·소속 비교용 정규화 — 공백 제거 + 소문자."""
    return re.sub(r"\s+", "", (s or "")).lower()


def _who(name, org):
    return name if not org else ("%s·%s" % (name, org))


def _split_turns(text, mark):
    """'본문 ──── 추가 질문 (시각) ──── 다음글' 을 [(시각, 글), ...] 로 자른다.
    첫 덩어리는 시각이 없다(최초 접수·최초 답변)."""
    if not text:
        return []
    out, when, buf = [], "", []
    for line in text.split("\n"):
        m = TURN_RE.match(line.strip())
        if m and m.group(1) == mark:
            out.append((when, "\n".join(buf).strip()))
            when, buf = m.group(2), []
        else:
            buf.append(line)
    out.append((when, "\n".join(buf).strip()))
    return [(w, t) for w, t in out if t]


def _pull_stamp(when, text):
    """본문 끝의 '⏱YYYY-MM-DD HH:MM' 줄을 떼어 시각으로 돌린다."""
    lines = (text or "").rstrip().split("\n")
    if lines and lines[-1].startswith("⏱"):
        return (lines[-1][1:].strip() or when), "\n".join(lines[:-1]).rstrip()
    return when, text


def _turns(item):
    """문의·답변을 주고받은 시간순으로 편다. 현업 화면이 대화처럼 그린다."""
    qs = _split_turns(_strip_internal(item.get("q", "")), "추가 질문")
    ans = [_pull_stamp(w, t) for w, t in
           _split_turns(_strip_internal(item.get("a", "")), "추가 답변")]
    turns = []
    qi, ai = 0, 0
    if qs:
        turns.append({"role": "q", "when": qs[0][0], "text": qs[0][1]})
        qi = 1
    while qi < len(qs) or ai < len(ans):
        if ai >= len(ans):
            turns.append({"role": "q", "when": qs[qi][0], "text": qs[qi][1]})
            qi += 1
        elif qi >= len(qs):
            turns.append({"role": "a", "when": ans[ai][0], "text": ans[ai][1]})
            ai += 1
        else:
            wq, wa = (qs[qi][0] or "").strip(), (ans[ai][0] or "").strip()
            if wq and wa:
                if wq < wa or (wq == wa and qi <= ai):
                    turns.append({"role": "q", "when": qs[qi][0], "text": qs[qi][1]})
                    qi += 1
                else:
                    turns.append({"role": "a", "when": ans[ai][0], "text": ans[ai][1]})
                    ai += 1
            elif not wa:
                if ai < qi:
                    turns.append({"role": "a", "when": ans[ai][0], "text": ans[ai][1]})
                    ai += 1
                else:
                    turns.append({"role": "q", "when": qs[qi][0], "text": qs[qi][1]})
                    qi += 1
            else:
                if qi <= ai:
                    turns.append({"role": "q", "when": qs[qi][0], "text": qs[qi][1]})
                    qi += 1
                else:
                    turns.append({"role": "a", "when": ans[ai][0], "text": ans[ai][1]})
                    ai += 1
    return turns


def _followup_count(item):
    return (item.get("q") or "").count(Q_MARK)


# ── 문의자 종결(2026-10-06) — 내 문의함 [케이스 닫기] ──────────────────────────
# 기록하는 것은 상태 「완료」와 req_closed(닫은 시각) 뿐이다. 판정 기준은 stale_cases.py 와 같다(시험이 대조).
NEED_INFO_MARK = "정확한 안내를 위해 아래 정보가 필요합니다"   # 트리니티봇이 정보를 되묻는 글의 첫 줄 — vacation/apply_answer.py 의 값과 같다
CLOSED_LABEL = "문의자 종결"
NEED_INFO_LABEL = "추가 정보 필요"
ACK_HEAD = "[트리니티봇 · 접수 확인"          # 보류 답의 머리 표기 — vacation/apply_answer.py 의 MARK_ACK 앞부분(시험이 대조)
CLOSE_ASK_KEY = "해결되셨으면 아래 [케이스 닫기]를 눌러 주십시오"   # 게이트가 확정 답변 끝에 붙이는 종결 질문 — stale_cases.py·apply_answer.py 와 같다(시험이 대조)
Q_STAMP_RE = re.compile(r"──── 추가 질문 \(([^)]*)\) ────")


def _last_q_stamp(item):
    st = Q_STAMP_RE.findall(item.get("q") or "")
    return st[-1] if st else ""


def _req_closed(item):
    """문의자가 닫은 상태인가. 닫은 뒤 추가 질문이 붙으면 다시 열린 것이다.
    로그 병합은 빈 값으로 지우지 못하므로 req_closed 와 마지막 추가 질문 시각을 비교한다."""
    rc = item.get("req_closed") or ""
    if not rc or (item.get("status") or "") != "완료":
        return False
    return rc >= _last_q_stamp(item)


def _answer_tail(a):
    """마지막 답 블록의 본문. 「──── 추가 답변 (시각) ────」 줄은 떼고 봇 머리 표기부터 돌려준다.
    (2026-10-06 실서버 시험에서 발견: 추가 답변의 마지막 블록은 시각 줄로 시작해 머리 표기 검사가 빗나갔다)"""
    last = (a or "").split(A_MARK)[-1]
    return re.sub(r"^\s*\([^)]*\)\s*────\s*", "", last).lstrip()


def _can_close(item):
    """답이 달린 「대기」 건만 닫을 수 있다 — 답이 아직 없으면(확인 중) 닫지 않는다. 봇 답은 확정 답변과 보류만 닫는다."""
    a = (item.get("a") or "").strip()
    if (item.get("status") or "") != "대기" or not a:
        return False
    if _label(item) == "확인 중":              # 접수 확인만 있거나 새 추가 질문에 아직 답이 없다
        return False
    if a.startswith(BOT_HEADS):
        # 트리니티봇 답은 ① 종결 질문을 붙인 확정 답변 ② 보류(접수 확인 표기 — 정보 되묻기·추정 보고)일 때 닫을 수 있다.
        # 에스컬레이션·개발 요청은 담당자가 봐야 해서 문의자가 닫으면 목록에서 사라지므로 제외한다.
        # ②는 사용자 결정(2026-10-06): 문의자가 닫아도 복귀 보고서가 「보류 중 닫음」으로 따로 알린다.
        last = _answer_tail(a)
        return CLOSE_ASK_KEY in last or last.startswith(ACK_HEAD)
    return True        # 담당자가 직접 답한 건


def _append_followup(iid, text):
    """본인 문의에 추가 질문을 덧붙이고 상태를 '접수'로 되돌린다.

    ⚠️ 읽고-고쳐-쓰기다. 헬퍼가 파일 잠금을 잡지만 이 함수의 읽기~쓰기 사이는 보호되지 않는다.
       분당 접수 제한과 문의당 상한이 실질적 방어다. 동시 추가 질문이 잦아지면 헬퍼에
       '이어붙이기' 옵션을 만드는 게 맞다.
    """
    rec = _read_log().get(iid) or {}
    cur = rec.get("q") or ""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_q = "%s\n\n%s (%s) ────\n%s" % (cur, Q_MARK, stamp, text)
    cmd = [sys.executable, HELPER, "--id", iid, "--q", new_q, "--status", "접수"]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         env=env, cwd=HERE, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "추가 실패").strip()[:300])
    # 반영 확인 — 성공 메시지를 믿지 않는다
    again = _read_log().get(iid) or {}
    if _followup_count(again) != _followup_count(rec) + 1:
        raise RuntimeError("추가 질문이 반영되지 않았습니다")
    return True


def _close_case(iid, score=None, feedback=""):
    """본인 문의에 종결 표시를 남긴다(상태 완료 + req_closed + 점수/피드백). 읽고 확인까지 한다."""
    cmd = [sys.executable, HELPER, "--id", iid, "--req-close"]
    if score is not None and score in (1, 2, 3, 4, 5):
        cmd += ["--req-score", str(score)]
    if feedback:
        cmd += ["--req-feedback", str(feedback)]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         env=env, cwd=HERE, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "종결 실패").strip()[:300])
    if not _req_closed(_read_log().get(iid) or {}):
        raise RuntimeError("종결이 반영되지 않았습니다")
    return True


def _shot_dir(iid, make=False):
    d = os.path.join(SHOT_DIR, iid)
    if make:
        os.makedirs(d, exist_ok=True)
    return d


def _shot_list(iid):
    """이 문의에 붙은 첨부 목록. 파일명은 서버가 정한다(원본 이름을 믿지 않는다)."""
    d = _shot_dir(iid)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        ext = fn.rsplit(".", 1)[-1].lower()
        if ext in SHOT_TYPES and fn.split(".")[0].isdigit():
            try:
                out.append({"n": int(fn.split(".")[0]), "ext": ext,
                            "size": os.path.getsize(os.path.join(d, fn))})
            except OSError:
                pass
    return out


def _sniff(blob):
    """파일 **내용**으로 형식을 가린다 — 보낸 쪽이 적은 확장자·형식은 믿지 않는다."""
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if blob.startswith(b"GIF87a") or blob.startswith(b"GIF89a"):
        return "gif"
    if len(blob) > 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


def _decode_shots(raw, room):
    """요청에 실려 온 그림을 전부 검증해 [(ext, bytes), ...] 로 돌려준다.

    ⚠️ **글을 기록하기 전에** 부른다. 그림이 하나라도 불량이면 글도 넣지 않는다 —
       글만 들어가고 그림이 빠지면 글 속 [[사진:N]] 이 빈 자리를 가리키게 된다.
    """
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise ValueError("사진 정보를 읽지 못했습니다.")
    if len(raw) > room:
        raise ValueError("사진은 한 문의에 %d장까지 넣으실 수 있습니다 (지금 넣을 수 있는 자리 %d장)."
                         % (SHOT_MAX_COUNT, room))
    out = []
    for i, it in enumerate(raw, 1):
        data = it.get("data") if isinstance(it, dict) else ""
        try:
            blob = base64.b64decode(str(data or ""), validate=True)
        except Exception:
            raise ValueError("%d번째 사진을 해석하지 못했습니다." % i)
        if not blob:
            raise ValueError("%d번째 사진이 비어 있습니다." % i)
        if len(blob) > SHOT_MAX_BYTES:
            raise ValueError("%d번째 사진이 4MB 를 넘습니다." % i)
        ext = _sniff(blob)
        if not ext:
            raise ValueError("%d번째 파일은 그림이 아닙니다 (png·jpg·gif·webp)." % i)
        out.append((ext, blob))
    return out


def _shot_base(iid, text=""):
    """다음 그림 번호의 기준 = 폴더에 남은 파일과 **글 속 표시 중 큰 쪽**.

    ⚠️ 종결 뒤 그림이 지워진 문의에 추가 질문이 붙을 때, 폴더만 보고 1번부터 다시 매기면
       예전 글의 [[사진:1]] 이 새 그림을 가리키게 된다. **한 번 쓴 번호는 다시 쓰지 않는다.**
    """
    nums = [c["n"] for c in _shot_list(iid)] + [int(x) for x in TOKEN.findall(text or "")]
    return max(nums) if nums else 0


def _renumber(text, mapping):
    """[[사진:i]](보낸 쪽 순번) → [[사진:n]](서버 번호). 짝이 없는 표시는 지운다(없는 그림을 가리키지 않게)."""
    def sub(m):
        n = mapping.get(int(m.group(1)))
        return ("[[사진:%d]]" % n) if n else ""
    return TOKEN.sub(sub, text or "")


def _shot_write(iid, num, ext, blob):
    r"""⛔ 파일명은 **서버가 번호로 짓는다.** 사용자가 준 이름을 경로에 쓰지 않는다
       (`..\..\windows\...` 같은 경로 탈출을 원천 차단)."""
    d = _shot_dir(iid, make=True)
    final = os.path.join(d, "%d.%s" % (num, ext))
    tmp = final + ".tmp"
    with io.open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, final)
    return final


def _shot_read(iid, num):
    for c in _shot_list(iid):
        if c["n"] == num:
            p = os.path.join(_shot_dir(iid), "%d.%s" % (num, c["ext"]))
            with io.open(p, "rb") as f:
                return SHOT_TYPES[c["ext"]], f.read()
    return None, None


def _shot_purge():
    """**완료된 문의의 첨부를 지운다.** 스크린샷이 쌓이면 용량을 잡아먹고,
    고객 화면이 찍힌 이미지를 계속 들고 있을 이유도 없다.
    문의 기록(inquiry-log.js)은 그대로 남으므로 이력이 사라지는 것은 아니다."""
    if not os.path.isdir(SHOT_DIR):
        return 0
    merged = _read_log()
    gone = 0
    for iid in os.listdir(SHOT_DIR):
        rec = merged.get(iid)
        # 로그에 없는 폴더(오래된 잔재)도 함께 정리한다
        if rec is not None and (rec.get("status") or "접수") != "완료":
            continue
        d = os.path.join(SHOT_DIR, iid)
        if not os.path.isdir(d):
            continue
        try:
            newest = max([os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)] or [0])
        except OSError:
            newest = 0
        if time.time() - newest < SHOT_GRACE_SEC:
            continue            # 막 들어온 그림 — 종결된 문의가 추가 질문으로 다시 열리는 중일 수 있다
        try:
            for fn in os.listdir(d):
                os.remove(os.path.join(d, fn))
            os.rmdir(d)
            gone += 1
        except OSError:
            pass
    return gone


def _purge_loop():
    while True:
        time.sleep(SHOT_PURGE_SEC)
        try:
            with _write_lock:                 # 막 쓰는 그림을 지우지 않게 쓰기와 줄을 세운다
                k = _shot_purge()
            if k:
                sys.stdout.write("  [-] 완료된 문의 %d건의 첨부를 삭제했습니다\n" % k)
                sys.stdout.flush()
        except Exception:
            pass


BOT_HEADS = ("[트리니티봇", "[담당자 부재중")


def _label(item):
    """현업 화면에 보일 상태 이름 (2026-09-11 사용자 지시).

    트리니티봇 답이 달린 「대기」는 「1차 답변 완료」로 보인다 — 그냥 「대기」라고 쓰면 답이 없는 줄 안다.
      · 마지막 답이 AI 1차 안내      → 「1차 답변 완료」
      · 마지막 답이 보류(담당자 확인) → 「담당자 확인 대기」
      · 접수 확인만 있거나, 새 추가 질문에 아직 답하지 않았으면 → 「확인 중」
    ⚠️ 답 턴을 세는 기준은 vacation/apply_answer.py 의 answer_turns·q_turns 와 같다(시험이 대조한다).
    실제 상태값은 바꾸지 않는다 — 대시보드·트리니티봇은 여전히 「대기」로 본다.
    """
    status = item.get("status") or "접수"
    if _req_closed(item):
        return CLOSED_LABEL
    a = item.get("a") or ""
    if status != "대기" or not a.startswith(BOT_HEADS):
        return status
    blocks = a.split(A_MARK)
    real = "⏱" in blocks[0] or "AI 1차 안내" in blocks[0]
    turns = len(blocks) if real else 0
    if turns == 0 or turns < 1 + (item.get("q") or "").count(Q_MARK):
        return "확인 중"
    if NEED_INFO_MARK in blocks[-1]:
        return NEED_INFO_LABEL
    return "1차 답변 완료" if "AI 1차 안내" in blocks[-1] else "담당자 확인 대기"


def _answer_rev(item):
    """마지막 답변의 지문 — 새 답변 알림이 「이미 본 답」을 가려내는 기준(2026-10-30).

    「답 개수 - 마지막 답 본문 해시」. 새 답이 붙거나 마지막 답이 바뀌면 값이 달라진다.
    내부 메모([[내부]])는 걷어낸 뒤 해시하므로 메모만 고쳐서는 알림이 가지 않는다.
    """
    a = _strip_internal(item.get("a", ""))
    if not a:
        return ""
    n = len(_split_turns(a, "추가 답변"))
    return "%d-%s" % (n, hashlib.sha1(_answer_tail(a).encode("utf-8")).hexdigest()[:10])


def _answer_fresh(item):
    """알릴 만한 답인가 — 접수 확인뿐이거나 새 추가 질문에 아직 답이 없는 「확인 중」은 아니다."""
    return bool((item.get("a") or "").strip()) and _label(item) != "확인 중"


def _answer_line(item):
    """알림 본문 — 마지막 답의 첫 줄(봇 머리 표기·시각·구분선은 건너뛴다)."""
    for ln in _answer_tail(_strip_internal(item.get("a", ""))).splitlines():
        t = ln.strip()
        if not t or t.startswith(BOT_HEADS) or t.startswith("⏱") or t.startswith("────"):
            continue
        return t[:100]
    return ""


_pulse_lock = threading.Lock()
_pulse_cache = {"sig": None, "recs": {}}


def _records_cached():
    """웹접수 기록 — 로그 파일(크기·수정시각)이 바뀔 때만 다시 읽는다.

    새 답변 확인은 문의자 PC마다 25초에 한 번 오므로, 매번 로그 전체(수백 KB)를 파싱하지 않게 한다.
    읽는 도중 파일이 바뀌어도 서명이 옛것이라 다음 호출에서 다시 읽는다(최악이 한 번 더 읽기).
    """
    try:
        st = os.stat(LOG_JS)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _pulse_lock:
        if _pulse_cache["sig"] != sig:
            _pulse_cache["recs"] = _web_records()
            _pulse_cache["sig"] = sig
        return _pulse_cache["recs"]


def _pulse(name, org):
    """본인 건의 「새 답변 확인용」 최소 정보 — id · 지문 · 알릴 답인지 · 알림 본문 한 줄."""
    want = _norm(_who(name, org))
    if not want:
        return []
    rows = []
    for v in _records_cached().values():
        if _norm(v.get("req", "")) != want:
            continue
        fresh = _answer_fresh(v)
        rows.append({"id": v.get("id", ""), "rev": _answer_rev(v), "fresh": fresh,
                     "line": _answer_line(v) if fresh else ""})
    rows.sort(key=lambda r: r["id"], reverse=True)
    return rows


def _public_view(item):
    """⛔ 공개 응답은 반드시 이 함수를 거친다 (설계 원칙 5).

    내보내는 필드: id · date · type · status · q · a  — 그 외는 전부 뺀다.
    특히 ref(아카이브 앵커)·by(기록 담당)·req(요청자 원문)는 내부 정보다.
    """
    shots = _shot_list(item.get("id", ""))
    return {
        "id": item.get("id", ""),
        "date": item.get("date", ""),
        "type": item.get("type", ""),
        "status": item.get("status", "") or "접수",
        "label": _label(item),
        "q": _strip_internal(item.get("q", "")),
        "a": _strip_internal(item.get("a", "")),
        "turns": _turns(item),
        "followups": _followup_count(item),
        "can_ask": _followup_count(item) < MAX_FOLLOWUPS,
        "closed": _req_closed(item),
        "can_close": _can_close(item),
        "score": item.get("req_score"),
        "feedback": item.get("req_feedback", ""),
        "shots": [c["n"] for c in shots],
        "shot_left": max(0, SHOT_MAX_COUNT - len(shots)),
        "rev": _answer_rev(item),            # 새 답변 알림이 「본 답」을 기록하는 지문
        "fresh": _answer_fresh(item),
    }


_PUSH_START_RE = re.compile(r"^[ \t]*window\.INQUIRY_LOG\.push\(", re.M)


def _read_log():
    """inquiry-log.js 를 파싱해 id 별 최신 상태로 병합한 dict 를 돌려준다(단일줄 · 여러줄 push 모두 지원)."""
    if not os.path.exists(LOG_JS):
        return {}
    with io.open(LOG_JS, "r", encoding="utf-8") as f:
        raw = f.read()
    merged = {}
    dec = json.JSONDecoder()
    i, n = 0, len(raw)
    while i < n:
        m = _PUSH_START_RE.search(raw, i)
        if not m:
            break
        pos = m.end()
        while pos < n and raw[pos] in " \t\r\n":
            pos += 1
        try:
            obj, end_idx = dec.raw_decode(raw, pos)
        except Exception:
            nl = raw.find("\n", pos)
            i = n if nl < 0 else nl + 1
            continue
        if isinstance(obj, dict):
            rid = obj.get("id")
            if rid:
                cur = merged.setdefault(rid, {})
                for k, v in obj.items():
                    if v not in ("", None):
                        cur[k] = v
        i = end_idx
    return merged


def _web_records():
    """공개 대상 = 웹으로 접수된 기록뿐 (설계 원칙 6)."""
    return {k: v for k, v in _read_log().items() if v.get("by") == WEB_BY}


def _mine(name, org):
    """본인이 웹으로 접수한 건 목록 — 최신 접수순."""
    want = _norm(_who(name, org))
    if not want:
        return []
    rows = [v for v in _web_records().values() if _norm(v.get("req", "")) == want]
    rows.sort(key=lambda r: r.get("id", ""), reverse=True)
    return [_public_view(r) for r in rows]


def _submit(req_type, name, org, question, role=""):
    """log-inquiry.py --new 를 호출하고 발급된 id 를 돌려준다."""
    cmd = [
        sys.executable, HELPER, "--new",
        "--type", req_type,
        "--q", question,
        "--by", WEB_BY,
        "--req", _who(name, org),
        "--date", datetime.now().strftime("%Y-%m-%d"),
    ] + (["--role", role] if role else [])
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         env=env, cwd=HERE, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "접수 실패").strip()[:300])
    m = re.search(r"NEW\s+id=(\S+)", out.stdout or "")
    if not m:
        raise RuntimeError("접수는 됐으나 조회코드를 읽지 못했습니다: %s" % (out.stdout or "")[:200])
    return m.group(1)


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SD 문의 접수</title>
<style>
 :root{--bg:#f6f7f9;--card:#fff;--line:#dfe3e8;--ink:#1c2430;--sub:#5b6675;--pt:#1a6fb5}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
      font-family:"Malgun Gothic","맑은 고딕",system-ui,sans-serif;font-size:14px;line-height:1.6}
 .wrap{max-width:760px;margin:0 auto;padding:26px 18px 60px}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:var(--sub);margin:0 0 18px;font-size:13px}
 .tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:18px}
 .tab{padding:9px 18px;cursor:pointer;font-weight:700;font-size:14px;color:var(--sub);
      border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;margin-bottom:-1px}
 .tab.on{background:var(--card);border-color:var(--line);color:var(--pt)}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
 label{display:block;font-weight:700;margin:14px 0 6px;font-size:13px}
 label:first-of-type{margin-top:0}
 input,select,textarea{width:100%;padding:9px 11px;border:1px solid var(--line);
      border-radius:6px;font:inherit;background:#fff;color:inherit}
 textarea{min-height:150px;resize:vertical}
 .row{display:flex;gap:12px}.row>div{flex:1}
 button{background:var(--pt);color:#fff;border:0;border-radius:6px;padding:11px 20px;
      font:inherit;font-weight:700;cursor:pointer;margin-top:18px}
 button:disabled{background:#9bb4c9;cursor:default}
 button.ghost{background:#fff;color:var(--pt);border:1px solid var(--pt);margin-top:0}
 .hint{color:var(--sub);font-size:12px;margin-top:6px}
 .ok{background:#eef7ee;border-color:#bcd9bc}
 .err{background:#fdefef;border-color:#e6bcbc}
 .code{font-family:Consolas,monospace;font-size:17px;font-weight:700;letter-spacing:.4px;
      background:#fff;border:1px dashed var(--pt);border-radius:6px;padding:9px 12px;display:inline-block;margin:8px 0}
 .st{display:inline-block;padding:2px 10px;border-radius:11px;font-size:12px;font-weight:700}
 .st-접수{background:#e8f0fb;color:#1a5fa8}
 .st-진행중{background:#fdf2df;color:#8a5a00}
 .st-대기{background:#eeeeee;color:#555}
 .st-완료{background:#e8f6ea;color:#1f7a33}
 .closebar{margin:10px 0 2px;padding:9px 12px;border:1px dashed #9bc7a3;border-radius:8px;background:#f4fbf5;font-size:13px;color:#245c2e}
 .closebar.done{border-style:solid;background:#eef7ef}
 .closebar .chead{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .closebar button{padding:5px 12px;border-radius:6px;border:1px solid #2f8f46;background:#2f8f46;color:#fff;font-weight:700;cursor:pointer}
 .closebar button:disabled{opacity:.5;cursor:default}
 .cform{margin-top:9px;padding-top:8px;border-top:1px dashed #b7dabf}
 .cform .crow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0}
 .cform select{width:auto;padding:4px 6px;font-size:12px;background:#fff;border:1px solid #9bc7a3;border-radius:5px}
 .cform input[type=text]{padding:5px 8px;font-size:12px;flex:1;min-width:180px;border:1px solid #9bc7a3;border-radius:5px}
 .item{border:1px solid var(--line);border-radius:9px;padding:14px 16px;margin-bottom:11px;background:#fff}
 .item .top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:7px}
 .item .id{font-family:Consolas,monospace;font-size:12px;color:var(--sub)}
 .item .q{font-weight:700;white-space:pre-wrap;word-break:break-word}
 .item .ans{margin-top:10px;padding:11px 13px;background:#f7f9fb;border-left:3px solid var(--pt);
      border-radius:0 6px 6px 0;white-space:pre-wrap;word-break:break-word}
 .item .ans b{display:block;margin-bottom:4px;color:var(--pt);font-size:11px}
 .item .none{margin-top:9px;color:var(--sub);font-size:13px}
 /* 대화 — 문의와 답변을 주고받은 순서대로 */
 .turn{margin-top:9px;padding:10px 12px;border-radius:8px;white-space:pre-wrap;
      word-break:break-word;font-size:13px;line-height:1.62}
 .turn.q{background:#eef1f5;border-left:3px solid #90a4b8}
 .turn.a{background:#f4f8fb;border-left:3px solid var(--pt)}
 .turn .who{font-size:11px;font-weight:700;margin-bottom:4px;color:var(--sub)}
 .turn.a .who{color:var(--pt)}
 .ask{margin-top:12px;padding-top:11px;border-top:1px dashed var(--line)}
 .ask textarea{min-height:62px}
 .ask .arow{display:flex;gap:8px;align-items:flex-start;margin-top:7px}
 .ask button{margin-top:0;padding:8px 16px;font-size:13px}
 .ask .hint{margin-top:0}
 .askmsg{font-size:12px;margin-top:7px}
 .askmsg.ok{color:#1f7a33}.askmsg.err{color:#b3261e}
 /* 접기 / 펼치기 */
 .item .top{cursor:pointer;user-select:none}
 .item .cy{display:inline-block;color:#9aa7b4;font-size:10px;transition:transform .15s;margin-right:5px}
 .item.open .cy{transform:rotate(90deg)}
 .item .peek{font-weight:700;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .item.open .peek{display:none}
 .item .fold{display:none}
 .item.open .fold{display:block}
 .empty{color:var(--sub);text-align:center;padding:26px 0;font-size:13px}
 /* 스크린샷 첨부 */
 .filebox{margin-top:6px}
 .filebox input[type=file]{padding:6px;font-size:12px;background:#fafbfc}
 .shots{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}
 .shots a{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);
      border-radius:7px;padding:5px 10px;font-size:12px;color:var(--pt);
      text-decoration:none;background:#fff}
 .shots a:hover{border-color:var(--pt);background:#f4f8fb}
 .shotmsg{font-size:12px;color:var(--sub);margin-top:5px}
 .turn img{max-width:100%;display:block;margin:8px 0;border-radius:8px;
      border:1px solid var(--line);cursor:zoom-in}
 /* 글과 사진을 함께 쓰는 입력칸 — 이메일·워드처럼 글 사이에 그림이 들어간다 */
 .ed{width:100%;min-height:160px;max-height:560px;overflow-y:auto;padding:9px 11px;
      border:1px solid var(--line);border-radius:6px;background:#fff;color:inherit;
      white-space:pre-wrap;word-break:break-word;outline:none;cursor:text;line-height:1.6}
 .ed:focus{border-color:var(--pt);box-shadow:0 0 0 2px rgba(26,111,181,.12)}
 .ed.blank:before{content:attr(data-ph);color:#9aa7b4;pointer-events:none}
 .ed img{max-width:100%;vertical-align:bottom;margin:4px 1px;border-radius:6px;border:1px solid var(--line)}
 .ed img:hover{outline:2px solid #bcd3e8}
 .ed.sm{min-height:74px}
 .edbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:7px}
 button.sm{padding:6px 12px;font-size:12px}
 .shotmsg.err{color:#b3261e}
 .gone{display:block;color:var(--sub);font-size:12px;margin:6px 0;font-style:italic}
 .st-ai{background:#e3f4ef;color:#11795e}
 /* 이용 안내 탭 */
 .guide h2{font-size:15px;margin:0 0 10px}
 .guide h2:not(:first-child){margin-top:22px}
 .guide ol,.guide ul{margin:0;padding-left:20px}
 .guide li{margin:0 0 7px}
 .guide table{width:100%;border-collapse:collapse;font-size:13px}
 .guide td{padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}
 .guide td:first-child{white-space:nowrap;width:1%}
 .guide .note{color:var(--sub);font-size:12px;margin-top:10px}
 a.guidelink{color:var(--pt);cursor:pointer;font-weight:700;text-decoration:underline}
</style></head><body><div class="wrap">

<h1>SD 문의 접수</h1>
<p class="sub">SAP SD 관련 문의·요청을 남기시면 담당자가 확인 후 답변드립니다.
  처음이시면 <a class="guidelink" id="guidelink">이용 안내</a>를 먼저 읽어 보세요.</p>

<div class="tabs">
  <div class="tab on" data-t="new">문의하기</div>
  <div class="tab" data-t="mine">내 문의함</div>
  <div class="tab" data-t="help">이용 안내</div>
</div>

<!-- ── 문의하기 ── -->
<div id="pane-new">
  <div class="card" id="formcard">
    <label>성함 · 소속</label>
    <div class="row">
      <div><input id="name" maxlength="40" placeholder="예: 홍길동" autocomplete="off"></div>
      <div><input id="org" maxlength="60" placeholder="소속 (예: 영업지원팀)" autocomplete="off"></div>
    </div>
    <div class="hint">성함과 소속으로 「내 문의함」을 찾습니다. 다음에도 <b>똑같이</b> 적어주세요.</div>

    <label>담당 업무</label>
    <input id="role" maxlength="40" list="rolelist" placeholder="예: 영업대표 · 영업지원 · 여신담당 · 재경" autocomplete="off">
    <datalist id="rolelist">
      <option value="영업대표"><option value="영업지원"><option value="여신담당">
      <option value="재경(회계·세금계산서)"><option value="구매"><option value="물류·출고">
    </datalist>
    <div class="hint">담당 업무에 따라 안내가 달라집니다. 예) 여신 한도 변경은 여신담당자만 할 수 있는 작업입니다.</div>

    <label>문의 유형</label>
    <select id="type">
      <option value="단순문의">단순문의 — 사용법·확인 요청</option>
      <option value="일반요청">일반요청 — 권한·설정·데이터 처리</option>
      <option value="긴급문의">긴급문의 — 업무가 멈춰 있음</option>
    </select>

    <label>문의 내용</label>
    <div id="q" class="ed" contenteditable="true" role="textbox" aria-multiline="true"
         data-msg="shotpick" data-msgcls="shotmsg"
         data-ph="어떤 화면에서 무엇을 하시려다 막히셨는지 적어주세요. 캡처한 화면은 글 사이 원하는 자리에 Ctrl+V 로 붙여넣으시면 됩니다."></div>
    <div class="edbar">
      <button type="button" class="ghost sm" id="pick">🖼 파일에서 사진 넣기</button>
      <input type="file" id="shots" accept="image/*" multiple style="display:none">
      <span class="shotmsg" id="shotpick"></span>
    </div>
    <div class="hint">화면(T-Code)·문서번호·오류 문구가 있으면 확인이 훨씬 빨라집니다.<br>
      캡처한 화면은 <b>글 사이 원하는 자리에 Ctrl+V</b> — 이메일처럼 글과 그림이 섞여 들어갑니다. 여러 장도 됩니다.
      문의당 최대 __SHOT_MAX__장 · 큰 사진은 자동으로 줄여서 넣습니다 · <b>문의가 종결되면 사진은 자동으로 삭제</b>됩니다.</div>

    <button id="send">등록하기</button>
  </div>
  <div class="card" id="result" style="display:none"></div>
</div>

<!-- ── 내 문의함 ── -->
<div id="pane-mine" style="display:none">
  <div class="card">
    <label>본인 확인</label>
    <div class="row">
      <div><input id="mname" maxlength="40" placeholder="성함" autocomplete="off"></div>
      <div><input id="morg" maxlength="60" placeholder="소속" autocomplete="off"></div>
      <div style="flex:0 0 96px"><button class="ghost" id="load" style="width:100%">조회</button></div>
    </div>
    <div class="hint">등록하실 때 적으신 성함·소속 그대로 입력하시면 본인이 신청하신 건만 보입니다.</div>
  </div>
  <div class="card" id="ntfbar">
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <div style="flex:0 0 130px"><button class="ghost" id="ntfbtn" style="width:100%">🔔 알림 켜기</button></div>
      <div class="hint" id="ntfmsg" style="margin-top:0;flex:1 1 240px"></div>
    </div>
  </div>
  <div id="mylist"></div>
</div>

<!-- ── 이용 안내 ── -->
<div id="pane-help" style="display:none">
  <div class="card guide">
    <h2>이렇게 쓰세요</h2>
    <ol>
      <li><b>「문의하기」</b>에 성함·소속·담당 업무를 적고 문의를 작성합니다. 화면 캡처는 글 사이에 <b>Ctrl+V</b>로 붙입니다.</li>
      <li><b>「내 문의함」</b>(성함·소속을 똑같이 입력)에서 답변을 확인합니다. 「내 문의함」의 <b>[알림 켜기]</b>를 누르면, <b>이 화면(탭)이 열려 있는 동안</b> 새 답변이 올라올 때 알려 드립니다(브라우저 알림 · 접속 주소가 http일 때는 탭 제목에 (1) 표시). <b>화면을 닫으면 알림이 가지 않으니</b> 이 주소를 즐겨찾기 해두시고, 닫아 두셨다면 다시 열어 확인해 주세요.</li>
      <li><b>「추가 정보 필요」</b>가 뜨면 필요한 정보를 <b>한 번에 모아</b> 추가 질문으로 보내 주세요. 그러면 정확히 안내해 드립니다.</li>
      <li>해결되셨으면 <b>[케이스 닫기]</b>를 눌러 주세요. 닫은 뒤에도 추가 질문을 남기면 다시 열립니다.</li>
    </ol>

    <h2>상태 표시</h2>
    <table>
      <tr><td><span class="st st-접수">접수</span> <span class="st st-접수">확인 중</span> <span class="st st-진행중">진행중</span></td><td>문의가 들어왔고 확인하는 중입니다.</td></tr>
      <tr><td><span class="st st-ai">1차 답변 완료</span></td><td>AI 가 먼저 안내를 드렸습니다. 해결되셨으면 [케이스 닫기], 더 필요하시면 추가 질문을 남겨 주세요.</td></tr>
      <tr><td><span class="st st-접수">추가 정보 필요</span></td><td>정확한 안내를 위해 확인할 내용이 있습니다. 답변에 적힌 항목을 한 번에 알려 주세요.</td></tr>
      <tr><td><span class="st st-대기">담당자 확인 대기</span></td><td>AI 가 확정하지 못해 담당자가 확인합니다. 담당자가 돌아오는 대로 답변드립니다.</td></tr>
      <tr><td><span class="st st-완료">문의자 종결</span> <span class="st st-완료">완료</span></td><td>마무리된 문의입니다. 닫은 뒤에도 추가 질문을 남기면 다시 열립니다.</td></tr>
    </table>

    <h2>알아 두실 점</h2>
    <ul>
      <li>답변 머리에 <b>「트리니티봇 · AI 1차 안내」</b>가 붙어 있으면 AI 가 먼저 드린 안내입니다. 확정 답변이 아니며, 담당자가 확인해 달라지는 부분이 있으면 정정드립니다.</li>
      <li>근거가 부족하면 <b>「추정입니다」</b>라고 밝히고, 케이스를 닫지 않은 채 담당자에게 보고하겠다고 안내합니다.</li>
      <li>운영 데이터의 직접 수정·삭제, 비밀번호·권한 부여는 안내만 드리고 담당자가 처리합니다. 여신 한도 변경 같은 기준정보 변경은 여신담당자에게 요청하셔야 합니다.</li>
      <li><b>비밀번호·개인정보는 적지 마세요.</b> 화면 캡처에 고객명·금액이 있으면 꼭 필요한 부분만 남겨 주세요.</li>
      <li>업무가 멈출 정도로 급한 건, 또는 이 화면이 열리지 않을 때는 기다리지 말고 <b>운영팀장님</b>께 직접 문의해 주세요.</li>
    </ul>
    <div class="note">문의 내용과 답변은 본인(성함·소속이 같은 사람)과 담당자만 볼 수 있고, 첨부 사진은 문의가 종결되면 자동으로 삭제됩니다.</div>
  </div>
</div>

<script>
var LS="sd-intake-me-v1";
var NL = String.fromCharCode(10);
var SHOT_MAX = __SHOT_MAX__;        // 문의 하나에 넣을 수 있는 사진 수 — 서버 값이 그대로 박힌다
var SHOT_BYTES = __SHOT_BYTES__;    // 사진 한 장 최대 크기(바이트) — 서버 값
var MAX_TEXT = __MAX_Q__;           // 글자 수 상한 — 서버 값
// [[사진:N]] — 대괄호를 문자 클래스로 적는다. 이 스크립트는 파이썬 문자열 안에 있어서
// 역슬래시를 쓰면 제어문자로 바뀌어 JS 가 통째로 죽는다(2026-09-10 실측).
var TOKEN_RE = "[[][[]사진:([0-9]+)]]";
function tokenRe(){ return new RegExp(TOKEN_RE, "g"); }
var ZWSP = new RegExp(String.fromCharCode(8203), "g");
var NBSP = new RegExp(String.fromCharCode(160), "g");
var OK_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
// 현업 화면 표기 → 배지 색. 실제 상태는 서버가 label 로 풀어서 준다(트리니티봇 답이 달린 「대기」 = 「1차 답변 완료」)
var LABEL_CLS = {"1차 답변 완료": "ai", "확인 중": "접수", "담당자 확인 대기": "대기", "추가 정보 필요": "접수", "문의자 종결": "완료"};

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function val(id){return (document.getElementById(id).value||"").trim();}
function set(id,v){document.getElementById(id).value=v||"";}

function me(){ try{ return JSON.parse(localStorage.getItem(LS)||"{}"); }catch(e){ return {}; } }
function remember(n,o,r){ try{ var m=me(); m.name=n; m.org=o; if(r!==undefined) m.role=r;
  localStorage.setItem(LS,JSON.stringify(m)); }catch(e){} }   // 담당 업무는 줄 때만 바꾼다(내 문의함 조회가 지우지 않게)

// ── 글과 사진을 함께 쓰는 입력칸 ────────────────────────────────────
// 이메일·워드처럼 **글 사이 원하는 자리에 그림이 들어간다.** 여러 장도 된다.
// 그림은 보내기 전까지 이 브라우저에만 있고, 보낼 때 **글과 한 요청으로** 함께 간다.
//   → 글은 들어갔는데 그림만 빠지거나, 그림 번호가 글과 어긋나는 일이 생기지 않는다.
// 보낼 때 그림 자리는 [[사진:N]] 표시가 된다. 담당자 대시보드·트리니티봇도 같은 표시를 본다.
var SHOTS = {};        // 그림 key → Blob
var shotSeq = 0;
var SENDING = {};      // 보내는 중인 추가 질문 (문의번호 → 1) — 두 번 보내기·목록 새로고침 중 재활성 방지
var DRAFTS = {};       // 목록을 다시 그릴 때 살려 둘 쓰던 추가 질문 (입력칸 id → 내용)
var loadSeq = 0;       // 겹친 목록 새로고침 중 늦게 온 옛 응답은 버린다

function edImgs(ed){ return Array.prototype.slice.call(ed.querySelectorAll("img[data-k]")); }

function edBlank(ed){
  var empty = !edImgs(ed).length && !(ed.textContent || "").replace(ZWSP, "").trim();
  ed.classList.toggle("blank", empty);
}

function edSaveRange(ed){
  var s = window.getSelection();
  if(s && s.rangeCount && ed.contains(s.anchorNode)) ed._range = s.getRangeAt(0).cloneRange();
}

// 지금 커서 자리. 커서가 입력칸 밖이면 마지막으로 있던 자리, 그것도 없으면 맨 끝.
function edRange(ed){
  var s = window.getSelection();
  if(s && s.rangeCount && ed.contains(s.anchorNode)) return s.getRangeAt(0);
  if(ed._range && ed.contains(ed._range.startContainer)) return ed._range;
  var r = document.createRange();
  r.selectNodeContents(ed); r.collapse(false);
  return r;
}

function edPlace(ed, r){
  ed.focus();
  var s = window.getSelection();
  s.removeAllRanges(); s.addRange(r);
  ed._range = r.cloneRange();
}

function edMsg(ed, text, bad){
  var m = document.getElementById(ed.getAttribute("data-msg") || "");
  if(!m) return;
  m.className = (ed.getAttribute("data-msgcls") || "shotmsg") + (bad ? " err" : "");
  m.textContent = text || "";
}

function edLeft(ed){
  var v = ed.getAttribute("data-left");
  return (v === null || v === "") ? SHOT_MAX : Number(v);
}

// 서식(글꼴·색·표)은 받지 않고 **글자만** 넣는다 — 보낼 때도 글자와 그림만 간다.
function edInsertText(ed, text){
  text = String(text).split(String.fromCharCode(13)).join("");
  if(!text) return;
  var done = false;
  try{ done = document.execCommand("insertText", false, text); }catch(e){ done = false; }
  if(done) return;
  var r = edRange(ed);
  r.deleteContents();
  var t = document.createTextNode(text);
  r.insertNode(t);
  var after = document.createRange();
  after.setStartAfter(t); after.collapse(true);
  edPlace(ed, after);
}

function edInsertImg(ed, blob){
  var key = "k" + (++shotSeq);
  SHOTS[key] = blob;
  var img = document.createElement("img");
  img.src = URL.createObjectURL(blob);
  img.setAttribute("data-k", key);
  img.alt = "";          // 비워 둔다 — 잘라내어 다른 곳에 붙일 때 「사진」 같은 글자가 대신 들어가지 않게
  var r = edRange(ed);
  r.deleteContents();
  r.insertNode(img);
  var after = document.createRange();
  after.setStartAfter(img); after.collapse(true);
  edPlace(ed, after);
}

function loadImg(blob){
  return new Promise(function(res, rej){
    var u = URL.createObjectURL(blob), im = new Image();
    im.onload = function(){ res(im); };
    im.onerror = function(){ URL.revokeObjectURL(u); rej(new Error("decode")); };
    im.src = u;
  });
}

function canvasBlob(c, type, q){
  return new Promise(function(res){ c.toBlob(function(b){ res(b); }, type, q); });
}

// 고해상도 모니터 캡처는 4MB 를 넘기 쉽다. 거절하지 않고 **글자가 읽히는 크기로 줄여서** 넣는다.
// 받지 않는 형식(bmp 등)도 여기서 png 로 바꾼다.
async function fitSize(blob){
  if(OK_TYPES.indexOf(blob.type) >= 0 && blob.size <= SHOT_BYTES) return blob;
  var im = await loadImg(blob);
  var w = im.naturalWidth, h = im.naturalHeight;
  var tries = [[4096, "image/png", 1], [2560, "image/png", 1], [2560, "image/jpeg", 0.9],
               [1920, "image/jpeg", 0.85], [1600, "image/jpeg", 0.8]];
  try{
    for(var i=0;i<tries.length;i++){
      var s = Math.min(1, tries[i][0] / Math.max(w, h, 1));
      var c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(w * s));
      c.height = Math.max(1, Math.round(h * s));
      var g = c.getContext("2d");
      g.fillStyle = "#ffffff"; g.fillRect(0, 0, c.width, c.height);
      g.drawImage(im, 0, 0, c.width, c.height);
      var b = await canvasBlob(c, tries[i][1], tries[i][2]);
      if(b && b.size <= SHOT_BYTES) return b;
    }
  } finally {
    try{ URL.revokeObjectURL(im.src); }catch(e){}
  }
  return null;
}

// 붙여넣거나 고른 그림을 **넣는 순간 이 페이지 메모리로 복사**해 둔다. 클립보드·원본 파일에 기댄 채로 두면
// 등록할 때 읽지 못할 수 있다(2026-09-11 사진 4장 등록 실패 — 요청이 서버에 닿지도 않았다).
async function ownBlob(b){
  var buf = await b.arrayBuffer();
  if(!buf || !buf.byteLength) throw new Error("empty");
  return new Blob([buf], {type: b.type || "image/png"});
}

async function edAddImages(ed, files){
  files = Array.prototype.slice.call(files || []);
  var put = 0, over = 0, bad = 0, unread = 0;
  // 커서 자리를 먼저 붙잡아 둔다 — 그림을 줄이는 동안 딴 데를 눌러도 원래 자리에 들어가게
  edPlace(ed, edRange(ed).cloneRange());
  for(var i=0;i<files.length;i++){
    var f = files[i];
    if(!f || (f.type || "").indexOf("image/") !== 0){ bad++; continue; }
    // 남은 자리는 매번 다시 센다 — 줄이는 사이에 다른 붙여넣기가 끼어들 수 있다
    if(edImgs(ed).length >= edLeft(ed)){ over++; continue; }
    if(f.size > SHOT_BYTES) edMsg(ed, "큰 사진을 줄이는 중…");
    var g = null;
    try{ g = await fitSize(f); }catch(e){ g = null; }
    if(!ed.isConnected) return;        // 그 사이 목록이 다시 그려졌다 — 사라진 칸에 넣지 않는다
    if(!g){ bad++; continue; }
    try{ g = await ownBlob(g); }catch(e){ g = null; }
    if(!ed.isConnected) return;
    if(!g){ unread++; continue; }
    edInsertImg(ed, g);
    put++;
  }
  edBlank(ed);
  var note = [];
  if(over) note.push("사진은 이 문의에 " + edLeft(ed) + "장까지라 " + over + "장은 넣지 못했습니다");
  if(bad) note.push(bad + "개는 그림이 아니거나 너무 커서 넣지 못했습니다");
  if(unread) note.push(unread + "장은 사진을 읽지 못했습니다 — 다시 붙여 넣어 주세요");
  edMsg(ed, note.join(" · "), note.length > 0);
}

function clipFiles(cd){
  var out = [];
  var list = (cd.files && cd.files.length) ? cd.files : null;
  if(list){
    for(var i=0;i<list.length;i++) if((list[i].type || "").indexOf("image/") === 0) out.push(list[i]);
    return out;
  }
  var items = cd.items || [];
  for(var j=0;j<items.length;j++){
    if(items[j].kind === "file"){
      var f = items[j].getAsFile();
      if(f && (f.type || "").indexOf("image/") === 0) out.push(f);
    }
  }
  return out;
}

// 입력칸 안에서 잘라내기·복사한 것을 다시 붙일 때 — 그림까지 되살린다.
// (글자만 받으면 Ctrl+X → Ctrl+V 로 옮긴 그림이 사라진다)
function edPasteOwn(ed, html){
  var doc = new DOMParser().parseFromString(html, "text/html");
  var tagged = Array.prototype.slice.call(doc.querySelectorAll("img[data-k]"));
  var own = tagged.filter(function(im){ return !!SHOTS[im.getAttribute("data-k")]; });
  if(!own.length){
    // 이 창에 없는 그림 — 다른 창·다른 탭에서 복사한 것. 글만 들어가고 사진이 조용히 사라지지 않게 알린다
    if(tagged.length) edMsg(ed, "다른 창에서 복사한 사진 " + tagged.length + "장은 옮길 수 없습니다 — 사진은 이 창에서 다시 붙여 넣어 주세요", true);
    return false;
  }
  var BLK = /^(DIV|P|LI|TR|H1|H2|H3|H4|H5|H6|BLOCKQUOTE|PRE)$/;
  var buf = [], full = 0;
  // 그림 바로 뒤 줄바꿈 하나는 그림이 한 줄을 차지해서 생긴 것이다 — 버린다(붙일 때마다 빈 줄이 늘지 않게)
  var skipNL = false;
  function nl(){ if(skipNL && !buf.length){ skipNL = false; return; } buf.push(NL); }
  function flush(){ var t = buf.join(""); buf = []; if(t) edInsertText(ed, t); }
  function walk(n){
    if(n.nodeType === 3){ if(n.nodeValue){ buf.push(n.nodeValue); skipNL = false; } return; }
    if(n.nodeType !== 1) return;
    var tag = n.nodeName;
    if(tag === "BR"){ nl(); return; }
    if(tag === "IMG"){
      var b = SHOTS[n.getAttribute("data-k") || ""];
      if(!b) return;
      if(edImgs(ed).length >= edLeft(ed)){ full++; return; }
      flush(); edInsertImg(ed, b); skipNL = true;
      return;
    }
    if(tag === "SCRIPT" || tag === "STYLE") return;
    var blk = BLK.test(tag);
    if(blk && buf.length && buf[buf.length - 1] !== NL) nl();
    for(var c = n.firstChild; c; c = c.nextSibling) walk(c);
    if(blk) nl();
  }
  walk(doc.body);
  while(buf.length && buf[buf.length - 1] === NL) buf.pop();
  flush();
  var note = [];
  if(tagged.length > own.length) note.push("다른 창에서 복사한 사진 " + (tagged.length - own.length) + "장은 옮기지 못했습니다 — 이 창에서 다시 붙여 넣어 주세요");
  if(full) note.push("사진은 이 문의에 " + edLeft(ed) + "장까지라 " + full + "장은 넣지 못했습니다");
  edMsg(ed, note.join(" · "), note.length > 0);
  return true;
}

// ① 입력칸에서 복사·잘라내기 — 그림이 섞인 선택이면 **우리가 직접** 클립보드에 글과 그림 표시를 적는다.
// 브라우저에 맡기면 그림 표시(data-k)가 다 실리지 않을 수 있다(2026-09-11 사용자 발견: 전체 선택 → 잘라내기 →
// 붙여넣기에서 첫 사진만 남음). 글만 고른 경우는 브라우저에 맡긴다.
// 이 창에서 마지막으로 복사·잘라낸 것 — 글(비교용)과 그림 표시가 든 HTML 원본
var CLIP = null;

// 클립보드 글 비교용 정규화 — 브라우저·OS 가 바꿔 놓을 수 있는 것(CR 줄바꿈 · NBSP · ZWSP)만 맞춘다
function edNormClip(t){
  t = String(t || "");
  t = t.split(String.fromCharCode(13)).join("");
  t = t.split(String.fromCharCode(160)).join(" ");
  t = t.split(String.fromCharCode(8203)).join("");
  return t.trim();
}

function edCopy(ed, e, isCut){
  var s = window.getSelection();
  if(!e.clipboardData || !s || !s.rangeCount) return;
  var r = s.getRangeAt(0);
  if(!ed.contains(r.commonAncestorContainer)) return;
  var box = document.createElement("div");
  box.appendChild(r.cloneContents());
  if(!box.querySelector("img[data-k]")) return;
  e.preventDefault();
  var plain = String(s);
  if(!edNormClip(plain)) plain = "[사진 " + box.querySelectorAll("img[data-k]").length + "장]";
  e.clipboardData.setData("text/html", box.innerHTML);
  e.clipboardData.setData("text/plain", plain);
  // 실제 크롬은 붙일 때 돌려주는 HTML 에서 그림 표시(data-k)를 떨어뜨렸다(2026-09-11 — 글만 남고 사진 전부 사라짐).
  // 그래서 원본을 페이지가 직접 보관하고, 붙여넣을 때 글이 같으면 이 보관본을 쓴다.
  CLIP = {text: edNormClip(plain), html: box.innerHTML};
  if(isCut){
    var ok = false;
    try{ ok = document.execCommand("delete"); }catch(x){ ok = false; }
    if(!ok) r.deleteContents();
    edBlank(ed);
  }
}

function dataUrlToBlob(dataUrl){
  try{
    var parts = dataUrl.split(",");
    var mime = (parts[0].match(/:(.*?);/) || [])[1] || "image/png";
    var bstr = atob(parts[1]);
    var n = bstr.length;
    var u8arr = new Uint8Array(n);
    while(n--){ u8arr[n] = bstr.charCodeAt(n); }
    return new Blob([u8arr], {type: mime});
  }catch(e){ return null; }
}

function edPaste(ed, e){
  var cd = e.clipboardData;
  if(!cd) return;
  var files = clipFiles(cd);
  var text = cd.getData("text/plain") || "";
  var html = cd.getData("text/html") || "";
  // 이 창에서 방금 복사·잘라낸 것이면(클립보드 글이 보관본과 같으면) 클립보드 HTML 대신 **보관본**을 쓴다
  if(CLIP && CLIP.text && edNormClip(text) === CLIP.text) html = CLIP.html;
  e.preventDefault();
  if(html.indexOf("data-k") >= 0 && edPasteOwn(ed, html)){ edBlank(ed); return; }

  // 외부 HTML 에서 복사한 <img> 추출 (files 가 비어 있는 브라우저 "이미지 복사" 대응)
  if(!files.length && html.indexOf("<img") >= 0){
    try{
      var doc = new DOMParser().parseFromString(html, "text/html");
      var imgs = doc.querySelectorAll("img");
      for(var i = 0; i < imgs.length; i++){
        var src = imgs[i].getAttribute("src") || "";
        if(src.indexOf("data:image/") === 0){
          var b = dataUrlToBlob(src);
          if(b) files.push(b);
        }
      }
    }catch(err){}
  }

  // 엑셀·오피스 표 복사 판별: 표 태그나 탭(\t) 구분이 포함된 경우 텍스트 우선
  var isOfficeTable = (html.indexOf("<table") >= 0 || html.indexOf("office:excel") >= 0 || text.indexOf("\t") >= 0);

  // 메신저(팀즈·사내 메신저) 등에서 이미지 복사 시 텍스트에 파일명/경로만 실린 경우
  var isFileNameOnly = files.length > 0 && (
    files.some(function(f){ return f.name && f.name === text.trim(); }) ||
    /^(image|img|screenshot|스크린샷|캡처|capture)[0-9_.-]*\\.(png|jpe?g|gif|webp|bmp)$/i.test(text.trim()) ||
    /^[a-zA-Z0-9_.-]+\\.(png|jpe?g|gif|webp|bmp)$/i.test(text.trim())
  );

  if(isOfficeTable){
    if(text.trim()) edInsertText(ed, text);
    else if(files.length) edAddImages(ed, files);
  } else if(isFileNameOnly){
    // 파일명만 실린 클립보드 텍스트는 버리고 실제 이미지만 삽입
    edAddImages(ed, files);
  } else if(files.length && text.trim()){
    // 글과 이미지가 함께 복사된 경우 둘 다 삽입
    edInsertText(ed, text);
    edAddImages(ed, files);
  } else if(files.length){
    edAddImages(ed, files);
  } else if(text.trim()){
    edInsertText(ed, text);
  }
  edBlank(ed);
}

function hasFiles(dt){
  if(!dt || !dt.types) return false;
  for(var i=0;i<dt.types.length;i++) if(dt.types[i] === "Files") return true;
  return false;
}

function dropRange(ed, e){
  var r = null;
  if(document.caretRangeFromPoint){
    r = document.caretRangeFromPoint(e.clientX, e.clientY);
  } else if(document.caretPositionFromPoint){
    var p = document.caretPositionFromPoint(e.clientX, e.clientY);
    if(p){ r = document.createRange(); r.setStart(p.offsetNode, p.offset); r.collapse(true); }
  }
  return (r && ed.contains(r.startContainer)) ? r : null;
}

function makeEditor(ed){
  if(ed._ready) return;
  ed._ready = true;
  var save = function(){ edSaveRange(ed); };
  ["keyup", "mouseup", "input", "focus"].forEach(function(ev){ ed.addEventListener(ev, save); });
  ed.addEventListener("input", function(){ edBlank(ed); });
  ed.addEventListener("paste", function(e){ edPaste(ed, e); });
  ed.addEventListener("copy", function(e){ edCopy(ed, e, false); });
  ed.addEventListener("cut", function(e){ edCopy(ed, e, true); });
  ed.addEventListener("dragstart", function(){ ed._dragging = true; });
  ed.addEventListener("dragend", function(){ ed._dragging = false; edBlank(ed); });
  ed.addEventListener("dragover", function(e){
    // 입력칸 안에서 그림을 옮기는 중이면 브라우저에 맡긴다(창 전체의 「파일 떨어뜨리기 막기」가 끼어들지 않게)
    if(ed._dragging){ e.stopPropagation(); return; }
    if(hasFiles(e.dataTransfer)) e.preventDefault();
  });
  ed.addEventListener("drop", function(e){
    if(ed._dragging){ e.stopPropagation(); return; }   // 제자리 옮기기 — 브라우저가 그림을 옮긴다
    var dt = e.dataTransfer;
    if(hasFiles(dt)){
      e.preventDefault();
      var files = Array.prototype.slice.call(dt.files || []);
      var r = dropRange(ed, e);
      if(r) edPlace(ed, r);
      edAddImages(ed, files);
      return;
    }
    // 밖에서 끌어온 글 — 서식 없이 글자만
    var t = dt ? dt.getData("text/plain") : "";
    e.preventDefault();
    var r2 = dropRange(ed, e);
    if(r2) edPlace(ed, r2);
    if(t) edInsertText(ed, t);
    edBlank(ed);
  });
  edBlank(ed);
}

// 입력칸 → 보낼 글 + 그림 목록. 그림 자리는 입력칸 안의 순서대로 [[사진:1]], [[사진:2]] …
// 그림은 한 줄을 차지한다(앞뒤 글과 줄을 바꾼다) — 받는 화면도 그림을 한 줄로 그린다.
function edSerialize(ed){
  var out = [], files = [], last = "", pend = false;
  var BLOCK = /^(DIV|P|LI|UL|OL|H1|H2|H3|H4|H5|H6|TR|TABLE|BLOCKQUOTE|PRE|SECTION|ARTICLE)$/;
  function put(t){
    if(!t) return;
    if(pend && t.charAt(0) !== NL) out.push(NL);
    pend = false;
    out.push(t);
    last = t.charAt(t.length - 1);
  }
  function brk(){
    if(out.length && last !== NL){ out.push(NL); last = NL; }
    pend = false;
  }
  function walk(n){
    if(n.nodeType === 3){ put(n.nodeValue.replace(ZWSP, "").replace(NBSP, " ")); return; }
    if(n.nodeType !== 1) return;
    var tag = n.nodeName;
    if(tag === "BR"){ out.push(NL); last = NL; pend = false; return; }
    if(tag === "IMG"){
      var b = SHOTS[n.getAttribute("data-k") || ""];
      if(!b) return;                     // 이 입력칸이 만든 그림이 아니면 보내지 않는다
      files.push(b);
      brk();
      out.push("[[사진:" + files.length + "]]");
      last = "]";
      pend = true;                       // 그림 다음 글은 새 줄에서 시작
      return;
    }
    if(tag === "SCRIPT" || tag === "STYLE") return;
    var block = BLOCK.test(tag);
    if(block) brk();
    for(var c = n.firstChild; c; c = c.nextSibling) walk(c);
    if(block) brk();
  }
  for(var c = ed.firstChild; c; c = c.nextSibling) walk(c);
  var text = out.join("").replace(new RegExp(NL + NL + NL + "+", "g"), NL + NL).trim();
  return {text: text, files: files};
}

function edClear(ed){
  edImgs(ed).forEach(function(img){
    try{ URL.revokeObjectURL(img.src); }catch(e){}
    delete SHOTS[img.getAttribute("data-k")];
  });
  ed.innerHTML = "";
  ed._range = null;
  edBlank(ed);
}

function readB64(blob){
  return new Promise(function(res, rej){
    var r = new FileReader();
    r.onload = function(){ res(String(r.result).split(",")[1] || ""); };
    r.onerror = function(){ rej(r.error || new Error("read")); };
    r.readAsDataURL(blob);
  });
}

function extOf(blob){
  var t = (blob.type || "").split("/")[1] || "png";
  return (t === "jpeg") ? "jpg" : t;
}

async function packShots(files){
  var out = [];
  for(var i=0;i<files.length;i++){
    var d = "";
    try{ d = await readB64(files[i]); }
    catch(e){ throw new Error("SHOT_READ:" + (i + 1) + ":" + ((e && e.name) || "read")); }
    if(!d) throw new Error("SHOT_READ:" + (i + 1) + ":empty");
    out.push({ext: extOf(files[i]), data: d});
  }
  return out;
}

// 등록·추가 질문이 서버 응답 없이 실패했을 때 — **어디서** 실패했는지 가려 알린다(원인 파악용 오류 내용 포함).
function sendErrMsg(e){
  var m = String((e && (e.message || e.name)) || e || "");
  if(m.indexOf("SHOT_READ:") === 0){
    var p = m.split(":");
    return p[1] + "번째 사진을 읽지 못했습니다. 그 사진을 지우고 다시 붙여 넣은 뒤 보내 주세요. (" + (p[2] || "") + ")";
  }
  return "서버에 연결하지 못했습니다. 잠시 후 다시 눌러 보시고, 계속되면 담당자에게 알려주세요. (" + m.slice(0, 80) + ")";
}

async function readJson(r){
  try{ return await r.json(); }
  catch(e){ return {ok:false, error:"서버가 요청을 받지 못했습니다 (" + r.status + "). 사진이 너무 크면 몇 장 빼고 다시 시도해 주세요."}; }
}

// 파일을 입력칸 밖에 떨어뜨리면 브라우저가 그 그림으로 넘어가 버려 쓰던 글이 날아간다 — 막는다
window.addEventListener("dragover", function(e){ if(hasFiles(e.dataTransfer)) e.preventDefault(); });
window.addEventListener("drop", function(e){ if(hasFiles(e.dataTransfer)) e.preventDefault(); });

var qEd = document.getElementById("q");
makeEditor(qEd);
document.getElementById("pick").onclick = function(){ edSaveRange(qEd); document.getElementById("shots").click(); };
document.getElementById("shots").addEventListener("change", function(e){
  var fl = Array.prototype.slice.call(e.target.files || []);
  e.target.value = "";
  edAddImages(qEd, fl);
});

// 등록 후 연달아 또 넣을 수 있게 폼을 되살린다.
// 성함·소속은 그대로 두고 문의 내용만 비운다 — 같은 사람이 연속으로 넣는 게 보통이다.
function resetForm(){
  var box=document.getElementById("result");
  box.style.display="none"; box.innerHTML="";
  document.getElementById("formcard").style.display="";
  edClear(qEd);
  edMsg(qEd, "");
  document.getElementById("type").selectedIndex=0;
  var b=document.getElementById("send");
  b.disabled=false; b.textContent="등록하기";
  qEd.focus();
  window.scrollTo({top:0,behavior:"smooth"});
}

// 탭
document.querySelectorAll(".tab").forEach(function(t){
  t.onclick=function(){
    document.querySelectorAll(".tab").forEach(function(x){x.classList.remove("on");});
    t.classList.add("on");
    var which = t.dataset.t, mine = which==="mine";
    ["new","mine","help"].forEach(function(k){
      document.getElementById("pane-"+k).style.display = (k===which)?"block":"none";
    });
    if(mine && val("mname")) loadMine();
    window.scrollTo({top:0});
  };
});
document.getElementById("guidelink").onclick = function(){
  document.querySelector(".tab[data-t=help]").onclick();
};

// 등록 — 글과 그림을 한 번에 보낸다
document.getElementById("send").onclick = async function(){
  var b=this, name=val("name"), org=val("org"), type=val("type"), role=val("role");
  if(b.disabled) return;
  var s = edSerialize(qEd);
  if(!name){alert("성함을 입력해 주세요.");return;}
  if(!org){alert("소속을 입력해 주세요. 「내 문의함」을 찾는 기준입니다.");return;}
  if(!role){alert("담당 업무를 입력해 주세요. 담당에 따라 안내가 달라집니다 (예: 영업대표, 여신담당).");return;}
  if(!s.text){alert("문의 내용을 입력해 주세요."); qEd.focus(); return;}
  if(s.text.length > MAX_TEXT){alert("문의 내용이 너무 깁니다 (" + s.text.length + "자 / " + MAX_TEXT + "자). 나눠서 남겨 주세요.");return;}
  if(s.files.length > SHOT_MAX){alert("사진은 한 문의에 " + SHOT_MAX + "장까지 넣으실 수 있습니다.");return;}
  b.disabled=true; b.textContent = s.files.length ? ("사진 " + s.files.length + "장과 함께 등록 중…") : "등록 중…";
  try{
    var shots = await packShots(s.files);
    var r = await fetch("/api/new",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({name:name,org:org,role:role,type:type,q:s.text,shots:shots})});
    var j = await readJson(r);
    var box = document.getElementById("result");
    box.style.display="block";
    if(j.ok){
      remember(name,org,role); set("mname",name); set("morg",org);
      edClear(qEd);
      document.getElementById("formcard").style.display="none";
      box.className="card ok";
      box.innerHTML="<b>등록되었습니다.</b><div class='hint'>문의번호입니다. "
        + "진행상황은 위의 <b>내 문의함</b>에서 확인하실 수 있습니다.</div>"
        + "<div class='code'>"+esc(j.id)+"</div>"
        + (j.shots ? "<div class='shotmsg'>사진 " + j.shots + "장이 글과 함께 들어갔습니다.</div>" : "")
        + (j.warn ? "<div class='shotmsg err'>" + esc(j.warn) + "</div>" : "")
        + "<div class='hint'>담당자 확인 후 답변드립니다. 급한 건은 메신저로 함께 알려주세요.</div>"
        + "<button class='ghost' id='again' style='margin-top:14px'>+ 새 문의 등록하기</button>";
      document.getElementById("again").onclick = resetForm;
    } else {
      box.className="card err";
      box.innerHTML="<b>등록하지 못했습니다.</b><div class='hint'>"+esc(j.error)+"</div>";
      b.disabled=false; b.textContent="등록하기";
    }
  }catch(e){
    try{ console.error("등록 실패", e); }catch(x){}
    alert(sendErrMsg(e));
    b.disabled=false; b.textContent="등록하기";
  }
};

// 내 문의함 — 본인이 신청한 건만 목록으로 (최신순)
async function loadMine(){
  var name=val("mname"), org=val("morg"), box=document.getElementById("mylist");
  if(!name || !org){ box.innerHTML="<div class='empty'>성함과 소속을 모두 입력해 주세요.</div>"; return; }
  remember(name,org);
  var my = ++loadSeq;
  // 쓰던 추가 질문(붙여넣은 그림 포함)은 다시 그려도 살려 둔다. 겹쳐 눌러도 잃지 않게 페이지 전역에 둔다.
  box.querySelectorAll(".ed[id]").forEach(function(ed){
    if(!ed.classList.contains("blank")) DRAFTS[ed.id] = ed.innerHTML;
    else if(!DRAFTS[ed.id]) edClear(ed);
  });
  box.innerHTML="<div class='empty'>불러오는 중…</div>";
  try{
    var r = await fetch("/api/mine?name="+encodeURIComponent(name)+"&org="+encodeURIComponent(org));
    var j = await readJson(r);
    if(my !== loadSeq) return;          // 더 나중에 누른 새로고침이 그리고 있다
    if(!j.ok){ box.innerHTML="<div class='empty'>"+esc(j.error)+"</div>"; return; }
    if(!j.items.length){
      box.innerHTML="<div class='empty'>신청하신 건이 없습니다.<br>"
        + "등록 때 적으신 성함·소속과 정확히 같아야 조회됩니다.</div>"; return;
    }
    box.innerHTML = j.items.map(renderItem).join("");
    var restored = {};
    box.querySelectorAll("[data-ask]").forEach(function(b){
      var id = b.getAttribute("data-ask");
      var ed = document.getElementById("fu-" + id);
      if(ed){
        if(DRAFTS[ed.id]){ ed.innerHTML = DRAFTS[ed.id]; delete DRAFTS[ed.id]; restored[ed.id] = 1; }
        makeEditor(ed);
      }
      b.onclick=function(){ sendFollowup(id); };
      if(SENDING[id]){
        b.disabled = true;
        var m = document.getElementById("fm-" + id);
        if(m){ m.className = "askmsg"; m.textContent = "보내는 중…"; }
      }
    });
    box.querySelectorAll("[data-pick]").forEach(function(b){
      var id = b.getAttribute("data-pick");
      var inp = document.getElementById("fp-" + id), ed = document.getElementById("fu-" + id);
      if(!inp || !ed) return;
      b.onclick = function(){ edSaveRange(ed); inp.click(); };
      inp.onchange = function(){
        var fl = Array.prototype.slice.call(inp.files || []);
        inp.value = "";
        edAddImages(ed, fl);
      };
    });
    box.querySelectorAll("[data-toggle-close]").forEach(function(b){
      var id = b.getAttribute("data-toggle-close");
      b.onclick = function(){
        var f = document.getElementById("cf-" + id);
        if(f) f.style.display = (f.style.display === "none") ? "block" : "none";
      };
    });
    box.querySelectorAll("[data-close]").forEach(function(b){
      var id = b.getAttribute("data-close");
      b.onclick = function(){ sendClose(id); };
      if(SENDING["c" + id]) b.disabled = true;
    });
    box.querySelectorAll("img.shot").forEach(function(im){
      im.onclick = function(){ window.open(im.src); };
      im.onerror = function(){
        var sp = document.createElement("span");
        sp.className = "gone";
        sp.textContent = "(사진을 불러오지 못했습니다 — 종결된 문의의 사진은 자동으로 지워집니다)";
        im.replaceWith(sp);
      };
    });
    // 헤더를 누르면 접혔다 펴진다. 가장 최근 건과, 쓰던 추가 질문이 있는 건은 펴 둔다.
    box.querySelectorAll(".item").forEach(function(el,i){
      el.querySelector(".top").onclick=function(){ el.classList.toggle("open"); };
      var ed = el.querySelector(".ed[id]");
      if(i===0 || (ed && restored[ed.id])) el.classList.add("open");
    });
    ntfMark(j.items);
    ntfJump();
  }catch(e){ if(my === loadSeq) box.innerHTML="<div class='empty'>서버에 연결하지 못했습니다.</div>"; }
}

function shotUrl(id, k){
  return "/api/shot?id=" + encodeURIComponent(id) + "&n=" + k
       + "&name=" + encodeURIComponent(val("mname"))
       + "&org=" + encodeURIComponent(val("morg"));
}

// 본문의 [[사진:N]] 자리에 실제 그림을 끼운다. 앞뒤 줄바꿈은 그림이 대신한다(빈 줄이 겹치지 않게).
// esc() 를 먼저 거쳐도 이 표시는 특수문자가 없어 그대로 살아남는다.
function imgify(escaped, id){
  var re = new RegExp("(" + NL + ")?" + TOKEN_RE + "(" + NL + ")?", "g");
  return escaped.replace(re, function(_, a, k){
    return "<img class='shot' src=" + JSON.stringify(shotUrl(id, k))
         + " alt=" + JSON.stringify("스크린샷 " + k) + ">";
  });
}

function renderItem(d){
  var turns = (d.turns && d.turns.length) ? d.turns
            : [{role:"q",when:"",text:d.q},{role:"a",when:"",text:d.a}];
  var used = {};
  var body = turns.filter(function(t){ return t.text; }).map(function(t,i){
    var m, re = tokenRe();
    while((m = re.exec(t.text))) used[m[1]] = 1;
    var who = t.role==="q"
      ? (i===0 ? "내 문의" : "추가 질문" + (t.when ? " · "+esc(t.when) : ""))
      : ("답변" + (t.when ? " · "+esc(t.when)+" (서울)" : ""));
    return "<div class='turn "+t.role+"'><div class='who'>"+who+"</div>"+imgify(esc(t.text), d.id)+"</div>";
  }).join("");
  if(!d.a) body += "<div class='none'>아직 답변이 등록되지 않았습니다.</div>";

  // 글 속에 자리가 없는 첨부(예전 방식으로 올린 것)만 따로 모아 보여준다
  var extra = (d.shots || []).filter(function(k){ return !used[String(k)]; });
  var shots = extra.length ? ("<div class='shots'>" + extra.map(function(k){
      return "<a href=" + JSON.stringify(shotUrl(d.id, k)) + " target='_blank'>📎 첨부 " + k + "</a>";
    }).join("") + "</div>") : "";

  var id = esc(d.id);
  var left = Math.max(0, Number(d.shot_left || 0));
  var ask = d.can_ask
    ? "<div class='ask'>"
      + "<div class='hint'>답변을 보고 더 궁금하신 점이 있으면 <b>여기에 이어서</b> 물어보세요. "
      + (left ? "캡처한 화면은 <b>글 사이 원하는 자리에 Ctrl+V</b> 하시면 그대로 들어갑니다 (이 문의에 " + left + "장 더). "
              : "이 문의에는 사진을 더 넣을 수 없습니다. ")
      + "지금까지의 문의·답변을 그대로 두고 이어집니다.</div>"
      + "<div class='ed sm' contenteditable='true' role='textbox' aria-multiline='true' id='fu-" + id + "'"
      + " data-left='" + left + "' data-msg='fm-" + id + "' data-msgcls='askmsg'"
      + " data-ph='이어서 여쭤볼 내용을 적어주세요.'></div>"
      + "<div class='arow'><button data-ask='" + id + "'>추가 질문 남기기</button>"
      + (left ? "<button type='button' class='ghost sm' data-pick='" + id + "'>🖼 파일에서 사진 넣기</button>"
              + "<input type='file' accept='image/*' multiple style='display:none' id='fp-" + id + "'>" : "")
      + "<span class='askmsg' id='fm-" + id + "'></span></div></div>"
    : "<div class='ask'><div class='hint'>이 문의에는 더 추가하실 수 없습니다. "
      + "새 문의로 남겨주세요.</div></div>";

  var starStr = d.score ? (" (만족도: " + "★".repeat(d.score) + "☆".repeat(Math.max(0, 5 - d.score)) + " " + d.score + "점" + (d.feedback ? " · " + esc(d.feedback) : "") + ")") : "";
  var closeBar = d.closed
    ? "<div class='closebar done'>✔ 문의자 종결" + starStr + " · 닫은 뒤에도 추가 질문을 남기시면 다시 열립니다.</div>"
    : (d.can_close
      ? "<div class='closebar'><div class='chead'><span>이 답변으로 해결되셨나요?</span>"
        + "<button type='button' data-toggle-close='" + id + "'>케이스 닫기 및 평가</button>"
        + "<span class='askmsg' id='cm-" + id + "'></span></div>"
        + "<div class='cform' id='cf-" + id + "' style='display:none'>"
        + "<div class='crow'><span>만족도:</span>"
        + "<select id='cs-" + id + "'>"
        + "<option value='5' selected>⭐⭐⭐⭐⭐ 5점 (매우 만족)</option>"
        + "<option value='4'>⭐⭐⭐⭐ 4점 (만족)</option>"
        + "<option value='3'>⭐⭐⭐ 3점 (보통)</option>"
        + "<option value='2'>⭐⭐ 2점 (아쉬움)</option>"
        + "<option value='1'>⭐ 1점 (틀림·불만족)</option>"
        + "</select>"
        + "<input type='text' id='cfb-" + id + "' maxlength='200' placeholder='트리니티봇 개선을 위한 한줄 의견 (선택)'>"
        + "<button type='button' data-close='" + id + "'>종결 완료</button>"
        + "</div></div></div>"
      : "");
  var first = (d.turns && d.turns.length) ? d.turns[0].text : (d.q||"");
  var peek = first.replace(tokenRe(), " 🖼 ").split(NL)
                  .map(function(x){ return x.trim(); }).filter(Boolean).join(" ");
  var pch = Array.from(peek);           // 글자 단위로 자른다 — 🖼 같은 그림 문자가 반쪽 나지 않게
  if(pch.length > 42) peek = pch.slice(0,42).join("") + "…";
  var last = null;
  (d.turns||[]).forEach(function(t){ if(t.role==="a" && t.when) last = t.when; });

  return "<div class='item' id='it-" + id + "'><div class='top'>"
    + "<span class='id'><span class='cy'>▶</span>"+id+" · "+esc(d.date)+" · "+esc(d.type)
    + (last ? " · 답변 "+esc(last) : "") + "</span>"
    + "<span class='st st-"+esc(LABEL_CLS[d.label] || d.status)+"'>"+esc(d.label || d.status)+"</span></div>"
    + "<div class='peek'>"+esc(peek)+"</div>"
    + "<div class='fold'>" + body + shots + closeBar + ask + "</div></div>";
}

async function sendFollowup(id){
  if(SENDING[id]) return;                       // 이미 보내는 중 — 두 번 보내지 않는다
  var ed = document.getElementById("fu-"+id), msg = document.getElementById("fm-"+id);
  var btn = document.querySelector("[data-ask='" + id + "']");
  var s = edSerialize(ed);
  if(!s.text){ msg.className="askmsg err"; msg.textContent="내용을 입력해 주세요."; return; }
  if(s.text.length > MAX_TEXT){ msg.className="askmsg err"; msg.textContent="너무 깁니다 (" + s.text.length + "자 / " + MAX_TEXT + "자)."; return; }
  if(s.files.length > edLeft(ed)){ msg.className="askmsg err"; msg.textContent="이 문의에는 사진을 " + edLeft(ed) + "장까지만 더 넣으실 수 있습니다."; return; }
  SENDING[id] = 1;
  msg.className="askmsg"; msg.textContent = s.files.length ? ("사진 " + s.files.length + "장과 함께 보내는 중…") : "보내는 중…";
  if(btn) btn.disabled = true;
  var ok = false, err = "";
  try{
    var shots = await packShots(s.files);
    var r = await fetch("/api/followup",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:id,name:val("mname"),org:val("morg"),text:s.text,shots:shots})});
    var j = await readJson(r);
    ok = !!j.ok; err = j.error || "";
  }catch(e){ try{ console.error("추가 질문 실패", e); }catch(x){} err = sendErrMsg(e); }
  delete SENDING[id];
  // 보내는 동안 목록이 다시 그려졌을 수 있다 — 지금 화면에 있는 칸·버튼을 다시 찾는다
  var cur = document.getElementById("fu-"+id), cmsg = document.getElementById("fm-"+id);
  var cbtn = document.querySelector("[data-ask='" + id + "']");
  if(cbtn) cbtn.disabled = false;
  if(ok){
    delete DRAFTS["fu-"+id];
    if(cur) edClear(cur);
    if(ed && ed !== cur) edClear(ed);
    if(cmsg){ cmsg.className="askmsg ok"; cmsg.textContent="추가되었습니다."; }
    setTimeout(loadMine,700);
  } else if(cmsg){ cmsg.className="askmsg err"; cmsg.textContent=err || "추가하지 못했습니다."; }
}
async function sendClose(id){
  if(SENDING["c" + id]) return;                  // 이미 닫는 중 — 두 번 보내지 않는다
  var sel = document.getElementById("cs-" + id);
  var fbin = document.getElementById("cfb-" + id);
  var score = sel ? parseInt(sel.value, 10) : 5;
  var feedback = fbin ? fbin.value.trim() : "";
  if(!confirm("이 문의를 " + score + "점으로 종결하시겠습니까? 닫은 뒤에도 추가 질문을 남기시면 다시 열립니다.")) return;
  SENDING["c" + id] = 1;
  var btn = document.querySelector("[data-close='" + id + "']"), msg = document.getElementById("cm-" + id);
  if(btn) btn.disabled = true;
  if(msg){ msg.className = "askmsg"; msg.textContent = "종결 처리 중…"; }
  var ok = false, err = "";
  try{
    var r = await fetch("/api/close",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:id,name:val("mname"),org:val("morg"),score:score,feedback:feedback})});
    var j = await readJson(r);
    ok = !!j.ok; err = j.error || "";
  }catch(e){ try{ console.error("케이스 닫기 실패", e); }catch(x){} err = sendErrMsg(e); }
  delete SENDING["c" + id];
  if(ok){ loadMine(); return; }
  var b2 = document.querySelector("[data-close='" + id + "']"), m2 = document.getElementById("cm-" + id);
  if(b2) b2.disabled = false;
  if(m2){ m2.className = "askmsg err"; m2.textContent = err || "닫지 못했습니다."; }
}
document.getElementById("load").onclick = loadMine;
["mname","morg"].forEach(function(id){
  document.getElementById(id).addEventListener("keydown",function(e){
    if(e.key==="Enter") loadMine();});
});

// ── 새 답변 알림 (2026-10-30) ───────────────────────────────────────
// 이 화면(탭)이 열려 있는 동안 25초마다 /api/pulse 로 「내 건의 마지막 답 지문」만 받아 본다.
// 이미 본 답은 localStorage 에 지문을 남겨 두어 새로고침해도 다시 알리지 않는다(저장소가 막히면 메모리로만).
// 알림 권한은 [알림 켜기]를 눌렀을 때만 묻는다. http 접속(보안 컨텍스트 아님)·권한 거부·미지원이면 탭 제목 표시로만.
// ⚠️ 이 스크립트는 파이썬 문자열 안이라 역슬래시를 쓰지 않는다.
var NTF_K = LS + ".ntf", SEEN_K = LS + ".seen";
var NTF = {mem:"", seen:null, jump:"", n:0, busy:false, timer:0, title:document.title};
var PULSE_MS = 25000;
function ntfOn(){ try{ var v = localStorage.getItem(NTF_K); if(v !== null) return v === "on"; }catch(e){} return NTF.mem === "on"; }
function ntfPref(on){ NTF.mem = on ? "on" : "off"; try{ localStorage.setItem(NTF_K, NTF.mem); }catch(e){} }
function seenGet(){ try{ var s = JSON.parse(localStorage.getItem(SEEN_K) || "null"); if(s && s.map) return s; }catch(e){} return NTF.seen; }
function seenPut(who, map){ var s = {who:who, map:map}; NTF.seen = s; try{ localStorage.setItem(SEEN_K, JSON.stringify(s)); }catch(e){} }
function ntfWho(){ var m = me(); return m.name ? (m.name + "|" + (m.org || "")) : ""; }
function ntfCan(){ return !!window.isSecureContext && ("Notification" in window); }
function ntfMode(){
  if(!ntfOn()) return "off";
  return (ntfCan() && Notification.permission === "granted") ? "popup" : "title";
}
function ntfDraw(){
  var mode = ntfMode(), t;
  document.getElementById("ntfbtn").textContent = mode === "off" ? "🔔 알림 켜기" : "🔕 알림 끄기";
  if(mode === "off") t = "새 답변이 등록되면 알려 드립니다. 단, 이 화면(탭)이 열려 있는 동안에만 확인하며, 화면을 닫으면 알림이 가지 않습니다.";
  else if(mode === "popup") t = "알림 켜짐 — 새 답변이 등록되면 PC 알림으로 알려 드립니다. 이 화면(탭)이 열려 있는 동안에만 동작하며, 닫으면 알림이 가지 않습니다.";
  else if(!window.isSecureContext) t = "알림 켜짐(탭 제목 표시) — 이 접속 주소(http)에서는 브라우저가 팝업 알림을 허용하지 않아, 새 답변이 오면 탭 제목에 (1) 로 표시합니다. 이 화면이 열려 있는 동안에만 동작합니다.";
  else if(!("Notification" in window)) t = "알림 켜짐(탭 제목 표시) — 이 브라우저는 팝업 알림을 지원하지 않아 새 답변이 오면 탭 제목에 (1) 로 표시합니다. 이 화면이 열려 있는 동안에만 동작합니다.";
  else t = "알림 켜짐(탭 제목 표시) — 브라우저에서 이 사이트의 알림이 차단되어 있어 탭 제목에 (1) 로 표시합니다. 주소창 왼쪽 아이콘에서 알림을 허용하면 팝업으로 바뀝니다. 이 화면이 열려 있는 동안에만 동작합니다.";
  document.getElementById("ntfmsg").textContent = t;
}
function ntfTitle(n){ NTF.n = n; document.title = n ? ("(" + n + ") 새 답변 · " + NTF.title) : NTF.title; }
function ntfAway(){ return document.hidden || !document.hasFocus(); }
document.addEventListener("visibilitychange", function(){ if(!document.hidden) ntfTitle(0); });
window.addEventListener("focus", function(){ ntfTitle(0); });

// 알림 클릭 → 이 창으로 와서 그 문의를 펼친다
function ntfGoto(id){
  NTF.jump = id;
  var tab = document.querySelector(".tab[data-t=mine]");
  if(tab && tab.onclick) tab.onclick();
}
function ntfJump(){
  var id = NTF.jump; if(!id) return;
  var el = document.getElementById("it-" + id);
  if(!el) return;
  NTF.jump = "";
  el.classList.add("open");
  try{ el.scrollIntoView({block:"center"}); }catch(e){}
}
function ntfPopup(title, body, tag, id){
  if(ntfMode() !== "popup") return false;
  try{
    var n = new Notification(title, {body: body, tag: tag});
    n.onclick = function(){
      try{ window.focus(); }catch(e){}
      try{ n.close(); }catch(e){}
      if(id) ntfGoto(id);
    };
    return true;
  }catch(e){ return false; }
}

// 목록을 그린 뒤 — 지금 화면에 보인 답은 「본 것」으로 기록한다
function ntfMark(items){
  if(!ntfOn()) return;
  var who = ntfWho(); if(!who) return;
  var map = {};
  items.forEach(function(d){ map[d.id] = d.rev; });
  seenPut(who, map);
  ntfTitle(0);
}
function ntfHandle(who, items){
  var s = seenGet(), map = {}, fresh = [];
  if(!s || s.who !== who){            // 처음이거나 다른 사람 — 지금 있는 답은 기준으로만 삼고 알리지 않는다
    items.forEach(function(d){ map[d.id] = d.rev; });
    seenPut(who, map);
    return;
  }
  map = s.map || {};
  items.forEach(function(d){ if(d.fresh && map[d.id] !== d.rev) fresh.push(d); });
  if(!fresh.length) return;
  fresh.forEach(function(d){ map[d.id] = d.rev; });
  seenPut(who, map);
  fresh.slice(0, 5).forEach(function(d){
    ntfPopup(d.id + " · 새 답변", d.line || "답변이 등록되었습니다. 내 문의함에서 확인해 주세요.", d.id + "|" + d.rev, d.id);
  });
  if(ntfAway()) ntfTitle(NTF.n + fresh.length);
  var m = me();
  if(val("mname") === m.name && val("morg") === (m.org || "")) loadMine();   // 화면 목록도 최신으로
}
async function ntfPulse(){
  var who = ntfWho();
  if(NTF.busy || !ntfOn() || !who) return;
  var m = me();
  NTF.busy = true;
  try{
    var r = await fetch("/api/pulse?name=" + encodeURIComponent(m.name) + "&org=" + encodeURIComponent(m.org || ""), {cache:"no-store"});
    var j = await readJson(r);
    if(j && j.ok) ntfHandle(who, j.items || []);
  }catch(e){}
  NTF.busy = false;
}
function ntfSchedule(){
  clearTimeout(NTF.timer); NTF.timer = 0;
  if(!ntfOn()) return;
  NTF.timer = setTimeout(async function(){ await ntfPulse(); ntfSchedule(); }, PULSE_MS + Math.floor(Math.random() * 3000));
}
document.getElementById("ntfbtn").onclick = async function(){
  if(ntfOn()){ ntfPref(false); ntfSchedule(); ntfTitle(0); ntfDraw(); return; }
  ntfPref(true);
  if(ntfCan() && Notification.permission === "default"){      // 권한은 이 버튼을 눌렀을 때만 묻는다
    try{ await new Promise(function(res){ var p = Notification.requestPermission(res); if(p && p.then) p.then(res, res); }); }catch(e){}
  }
  ntfDraw();
  ntfPopup("알림이 켜졌습니다", "새 답변이 등록되면 이렇게 알려 드립니다. (이 화면이 열려 있는 동안)", "sd-ntf-test", "");
  if(val("mname") && val("morg")) loadMine();                 // 지금 보이는 답을 기준(본 것)으로 삼는다
  ntfPulse();
  ntfSchedule();
};
ntfDraw();
if(ntfOn()){ ntfPulse(); ntfSchedule(); }

// 지난번 입력 복원
(function(){ var m=me(); if(m.role){ set("role",m.role); }
  if(m.name){ set("name",m.name); set("org",m.org);
  set("mname",m.name); set("morg",m.org); } })();
</script>
</div></body></html>
"""
# 서버 상수를 화면 스크립트에 박는다 — 화면과 서버가 서로 다른 숫자를 쓰지 않게
PAGE = (PAGE.replace("__SHOT_MAX__", str(SHOT_MAX_COUNT))
            .replace("__SHOT_BYTES__", str(SHOT_MAX_BYTES))
            .replace("__MAX_Q__", str(MAX_Q)))


class SingleServer(ThreadingHTTPServer):
    """⛔ **중복 기동을 막는다.**

    ThreadingHTTPServer 기본값(allow_reuse_address=1)이면 Windows 에서 **같은 포트에
    두 프로세스가 동시에 바인딩된다.** 그러면 옛 프로세스가 계속 옛 페이지를 서빙해,
    코드를 고치고 재기동해도 화면이 안 바뀐다(2026-09-10 실측 — 서버가 2개 떠 있었고
    JS 수정본이 몇 시간 동안 반영되지 않았다). 두 번째 기동은 **시끄럽게 실패해야** 한다.
    """
    allow_reuse_address = False


def _tls_context(cert=None, key=None):
    """인증서·키가 둘 다 있으면 서버용 TLS 문맥, 없으면 None(HTTPS 안 연다). 읽다가 실패하면 예외."""
    cert, key = cert or TLS_CERT, key or TLS_KEY
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


class TLSServer(SingleServer):
    """같은 Handler 를 TLS 로 서빙한다.

    ⚠️ 핸드셰이크는 **연결별 스레드**에서 시한(10초)을 두고 한다. accept 루프에서 하면
    TCP 만 붙이고 말이 없는 연결 하나가 모든 사용자를 막는다.
    """
    ctx = None

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def finish_request(self, request, client_address):
        try:
            request.settimeout(10)
            request.do_handshake()
            request.settimeout(None)
        except (ssl.SSLError, OSError):
            return                     # 인증서 거부·평문 접속·무응답 — 조용히 버린다
        super().finish_request(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "SDIntake/2.2"

    def log_message(self, fmt, *args):
        sys.stdout.write("  %s  %s\n" % (self.client_address[0], fmt % args))

    # ---------- 공통 ----------
    def _send(self, code, ctype, body, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        ip = self.client_address[0]

        if path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", PAGE)

        # 본인이 웹으로 접수한 건 목록
        if path == "/api/mine":
            if not _rate_ok(ip, "lookup", LOOKUP_MAX):
                return self._json(200, {"ok": False, "error": "잠시 후 다시 시도해 주세요."})
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0].strip()[:MAX_NAME]
            org = (qs.get("org") or [""])[0].strip()[:MAX_ORG]
            if not name or not org:
                return self._json(200, {"ok": False, "error": "성함과 소속을 모두 입력해 주세요."})
            return self._json(200, {"ok": True, "items": _mine(name, org)})

        # 새 답변 확인 — 화면이 열려 있는 동안 25초마다. 전용 한도(조회 한도를 갉아먹지 않게)
        if path == "/api/pulse":
            if not _rate_ok(ip, "pulse", PULSE_MAX):
                return self._json(200, {"ok": False, "error": "잠시 후 다시 시도해 주세요."})
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0].strip()[:MAX_NAME]
            org = (qs.get("org") or [""])[0].strip()[:MAX_ORG]
            if not name or not org:
                return self._json(200, {"ok": False, "error": "성함과 소속을 모두 입력해 주세요."})
            return self._json(200, {"ok": True, "items": _pulse(name, org)})

        # 접수번호 단건 조회 비활성화 (이름·소속 또는 사번 인증으로만 본인 문의 조회)
        if path == "/api/my":
            return self._json(403, {"ok": False, "error": "접수번호 단독 조회는 종료되었습니다. 이름·소속 또는 사번 로그인으로 확인해 주세요."})

        # 첨부 이미지 — 본인 확인을 통과해야 내려간다
        if path == "/api/shot":
            if not _rate_ok(ip, "img", IMG_MAX):
                return self._send(429, "text/plain; charset=utf-8", "잠시 후 다시 시도해 주세요.")
            qs = parse_qs(urlparse(self.path).query)
            iid = (qs.get("id") or [""])[0].strip().upper()
            name = (qs.get("name") or [""])[0].strip()[:MAX_NAME]
            org = (qs.get("org") or [""])[0].strip()[:MAX_ORG]
            try:
                num = int((qs.get("n") or ["0"])[0])
            except ValueError:
                num = 0
            item = _web_records().get(iid)
            if not item or _norm(item.get("req", "")) != _norm(_who(name, org)):
                return self._send(403, "text/plain; charset=utf-8", "본인 문의가 아닙니다.")
            ctype, blob = _shot_read(iid, num)
            if not blob:
                return self._send(404, "text/plain; charset=utf-8", "첨부를 찾지 못했습니다.")
            # 본인 확인을 통과한 그림 — 그 사람 브라우저에만 잠시 둔다(다시 그릴 때마다 받지 않게)
            return self._send(200, ctype, blob, cache="private, max-age=600")

        # ⛔ /board · /inquiry-log.js 는 서빙하지 않는다 (설계 원칙 4)
        return self._send(404, "text/plain; charset=utf-8", "없는 경로입니다.")

    # ---------- POST : 신규 접수 · 본인 문의에 추가 질문 · 본인 문의 종결 표시. 이 셋뿐 ----------
    def _body(self, limit=64 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            raise ValueError("본문 크기 오류")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _drain(self):
        """거절할 요청이라도 본문은 읽어 버린다 — 안 읽고 답하면 브라우저가 올리던 중에 연결이 끊겨
        「서버에 연결하지 못했습니다」만 보고 진짜 사유(잠시 후 다시 시도 등)를 못 본다."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            while 0 < n <= BODY_MAX:
                chunk = self.rfile.read(min(n, 1 << 20))
                if not chunk:
                    break
                n -= len(chunk)
        except Exception:
            pass

    def _followup(self, ip):
        if not _rate_ok(ip, "new", RATE_MAX):
            self._drain()
            return self._json(200, {"ok": False, "error": "잠시 후 다시 시도해 주세요."})
        try:
            data = self._body(limit=BODY_MAX)
        except Exception:
            return self._json(200, {"ok": False,
                                    "error": "요청을 읽지 못했습니다 (사진이 너무 크거나 많습니다)."})

        iid = str(data.get("id") or "").strip().upper()
        name = str(data.get("name") or "").strip()[:MAX_NAME]
        org = str(data.get("org") or "").strip()[:MAX_ORG]
        text = str(data.get("text") or "").strip()
        if len(text) > MAX_Q:
            return self._json(200, {"ok": False, "error": "너무 깁니다 (%d자 / %d자). 나눠서 남겨 주세요."
                                    % (len(text), MAX_Q)})

        if not re.match(r"^INQ-\d{6}-\d{6}$", iid):
            return self._json(200, {"ok": False, "error": "접수번호가 올바르지 않습니다."})
        if not text:
            return self._json(200, {"ok": False, "error": "추가로 여쭤보실 내용을 입력해 주세요."})

        item = _web_records().get(iid)
        # 본인 것만 — 성함·소속이 접수 때와 정확히 같아야 한다
        if not item or _norm(item.get("req", "")) != _norm(_who(name, org)):
            return self._json(200, {"ok": False,
                                    "error": "본인이 등록하신 문의에만 추가 질문을 남기실 수 있습니다."})
        if _followup_count(item) >= MAX_FOLLOWUPS:
            return self._json(200, {"ok": False,
                                    "error": "이 문의에는 더 추가하실 수 없습니다. 새 문의로 남겨주세요."})
        # 그림: 전부 검증한 뒤에야 쓴다. 번호는 한 번 쓴 번호를 다시 쓰지 않게 이어서 매긴다.
        try:
            shots = _decode_shots(data.get("shots"), SHOT_MAX_COUNT)
        except ValueError as e:
            return self._json(200, {"ok": False, "error": str(e)})
        with _write_lock:
            # 잠금 안에서 다시 읽는다 — 같은 문의에 두 창에서 동시에 보내도 번호·횟수가 겹치지 않게
            item = _web_records().get(iid) or item
            if _followup_count(item) >= MAX_FOLLOWUPS:
                return self._json(200, {"ok": False,
                                        "error": "이 문의에는 더 추가하실 수 없습니다. 새 문의로 남겨주세요."})
            room = max(0, SHOT_MAX_COUNT - len(_shot_list(iid)))
            if len(shots) > room:
                return self._json(200, {"ok": False,
                                        "error": "이 문의에는 사진을 %d장까지만 더 넣으실 수 있습니다." % room})
            base = _shot_base(iid, (item.get("q") or "") + "\n" + (item.get("a") or ""))
            text = _renumber(text, dict((i, base + i) for i in range(1, len(shots) + 1))).strip()
            if not text:
                return self._json(200, {"ok": False, "error": "추가로 여쭤보실 내용을 입력해 주세요."})
            written = []
            try:
                for i, (ext, blob) in enumerate(shots, 1):
                    written.append(_shot_write(iid, base + i, ext, blob))
                _append_followup(iid, text)
            except Exception as e:
                for p in written:                # 글이 안 들어갔으면 그림도 남기지 않는다
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                sys.stdout.write("  [!] 추가 질문 실패 %s: %s\n" % (iid, e))
                return self._json(200, {"ok": False,
                                        "error": "추가하지 못했습니다. 담당자에게 알려주세요."})
        sys.stdout.write("  [+] 추가 질문 %s  %s·%s%s\n"
                         % (iid, name, org, (" · 사진 %d장" % len(written)) if written else ""))
        return self._json(200, {"ok": True, "id": iid, "shots": len(written)})

    def _close(self, ip):
        """[케이스 닫기] — 본인 문의에 종결 표시만 남긴다. 내용·답변은 고치지 못한다."""
        if not _rate_ok(ip, "close", RATE_MAX * 4):
            self._drain()
            return self._json(200, {"ok": False, "error": "잠시 후 다시 시도해 주세요."})
        try:
            data = self._body(limit=4096)
        except Exception:
            return self._json(200, {"ok": False, "error": "요청을 읽지 못했습니다."})
        iid = str(data.get("id") or "").strip().upper()
        name = str(data.get("name") or "").strip()[:MAX_NAME]
        org = str(data.get("org") or "").strip()[:MAX_ORG]
        score_val = data.get("score")
        try:
            score = int(score_val) if score_val is not None else None
            if score not in (1, 2, 3, 4, 5):
                score = None
        except (ValueError, TypeError):
            score = None
        feedback = str(data.get("feedback") or "").strip()[:500]
        if not re.match(r"^INQ-\d{6}-\d{6}$", iid):
            return self._json(200, {"ok": False, "error": "접수번호가 올바르지 않습니다."})
        item = _web_records().get(iid)
        # 본인 것만 — 성함·소속이 접수 때와 정확히 같아야 한다(추가 질문과 같은 기준)
        if not item or _norm(item.get("req", "")) != _norm(_who(name, org)):
            return self._json(200, {"ok": False, "error": "본인이 등록하신 문의만 닫으실 수 있습니다."})
        with _write_lock:
            item = _web_records().get(iid) or item
            if _req_closed(item):
                return self._json(200, {"ok": True, "id": iid, "already": True})
            if not _can_close(item):
                return self._json(200, {"ok": False,
                                        "error": "답변이 등록된 뒤에 닫으실 수 있습니다."})
            try:
                _close_case(iid, score=score, feedback=feedback)
            except Exception as e:
                sys.stdout.write("  [!] 케이스 닫기 실패 %s: %s\n" % (iid, e))
                return self._json(200, {"ok": False, "error": "닫지 못했습니다. 담당자에게 알려주세요."})
        sys.stdout.write("  [+] 케이스 닫기 %s  %s·%s  ★%s  %s\n"
                         % (iid, name, org, score or "-", feedback[:40]))
        return self._json(200, {"ok": True, "id": iid})

    def do_POST(self):
        path = urlparse(self.path).path
        ip = self.client_address[0]
        if path == "/api/followup":
            return self._followup(ip)
        if path == "/api/close":
            return self._close(ip)
        if path != "/api/new":
            return self._json(404, {"ok": False, "error": "없는 경로입니다."})
        if not _rate_ok(ip, "new", RATE_MAX):
            self._drain()
            return self._json(200, {"ok": False,
                                    "error": "잠시 후 다시 시도해 주세요 (연속 등록 제한)."})

        try:
            data = self._body(limit=BODY_MAX)
        except Exception:
            return self._json(200, {"ok": False,
                                    "error": "요청을 읽지 못했습니다 (사진이 너무 크거나 많습니다)."})

        name = str(data.get("name") or "").strip()[:MAX_NAME]
        org = str(data.get("org") or "").strip()[:MAX_ORG]
        rtype = str(data.get("type") or "").strip()
        q = str(data.get("q") or "").strip()
        # 담당 업무 — 화면은 필수로 받지만 서버는 비어 있어도 받는다(업데이트 전 열어 둔 화면에서 온 접수를 잃지 않게).
        # 비어 있으면 트리니티봇이 필요할 때 되묻는다(운영규칙 §3).
        role = str(data.get("role") or "").strip()[:MAX_ROLE]

        if not name:
            return self._json(200, {"ok": False, "error": "성함을 입력해 주세요."})
        if not org:
            return self._json(200, {"ok": False, "error": "소속을 입력해 주세요."})
        if not q:
            return self._json(200, {"ok": False, "error": "문의 내용을 입력해 주세요."})
        if rtype not in TYPES:
            rtype = "단순문의"
        if len(q) > MAX_Q:
            return self._json(200, {"ok": False, "error": "문의 내용이 너무 깁니다 (%d자 / %d자). 나눠서 남겨 주세요."
                                    % (len(q), MAX_Q)})
        # 그림이 하나라도 불량이면 글도 넣지 않는다(번호 어긋남 방지)
        try:
            shots = _decode_shots(data.get("shots"), SHOT_MAX_COUNT)
        except ValueError as e:
            return self._json(200, {"ok": False, "error": str(e)})
        q = _renumber(q, dict((i, i) for i in range(1, len(shots) + 1))).strip()
        if not q:
            return self._json(200, {"ok": False, "error": "문의 내용을 입력해 주세요."})

        with _write_lock:
            try:
                rid = _submit(rtype, name, org, q, role)
            except Exception as e:
                sys.stdout.write("  [!] 접수 실패: %s\n" % e)
                return self._json(200, {"ok": False,
                                        "error": "등록 처리 중 오류가 났습니다. 담당자에게 알려주세요."})
            saved, warn = 0, ""
            if _shot_list(rid):
                # 있을 수 없는 일(헬퍼가 빈 번호를 고른다) — 남의 그림과 섞이지 않게 쓰지 않고 알린다
                sys.stdout.write("  [!] 새 문의 %s 의 그림 폴더가 이미 있습니다 — 그림을 쓰지 않았습니다\n" % rid)
                warn = "사진을 저장하지 못했습니다. 담당자에게 알려주세요."
                shots = []
            for i, (ext, blob) in enumerate(shots, 1):
                try:
                    _shot_write(rid, i, ext, blob)
                    saved += 1
                except OSError as e:
                    sys.stdout.write("  [!] 사진 저장 실패 %s #%d: %s\n" % (rid, i, e))
                    warn = "사진 일부를 저장하지 못했습니다. 담당자에게 알려주세요."
        sys.stdout.write("  [+] 접수 %s  %s·%s%s  (%s)%s\n"
                         % (rid, name, org, (" [%s]" % role) if role else "", rtype,
                            (" · 사진 %d장" % saved) if saved else ""))
        return self._json(200, {"ok": True, "id": rid, "shots": saved, "warn": warn})


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    if not os.path.exists(HELPER):
        print("  [X] log-inquiry.py 를 찾지 못했습니다: %s" % HELPER)
        return 1
    ip = _lan_ip()
    print("")
    print("  SD 문의 접수 서버")
    print("  ------------------------------------------------------")
    print("  현업 화면 : http://%s:%d/     (접수 · 내 문의함)" % (ip, PORT))
    print("")
    print("  · 쓰기는 「신규 접수」 하나뿐입니다. 기존 카드는 수정되지 않습니다.")
    print("  · 담당자 대시보드·AI 참조 로그는 이 서버로 나가지 않습니다.")
    print("  · 현업은 본인이 접수한 건만 봅니다.")
    print("  · 스크린샷 첨부는 문의가 **완료되면 자동으로 삭제**됩니다.")
    print("  · 종료하려면 이 창에서 Ctrl+C 를 누르세요.")
    print("")
    try:
        srv = SingleServer((HOST, PORT), Handler)
    except OSError as e:
        print("  [X] 포트 %d 를 열 수 없습니다: %s" % (PORT, e))
        print("      이미 떠 있는 창이 없는지 확인하세요 (9_문의접수서버_종료.bat).")
        return 1

    try:
        k = _shot_purge()
        if k:
            print("  기동 시 완료된 문의 %d건의 첨부를 정리했습니다." % k)
    except Exception:
        pass
    threading.Thread(target=_purge_loop, daemon=True).start()

    try:
        ctx = _tls_context()
        if ctx:
            tsrv = TLSServer((HOST, TLS_PORT), Handler)
            tsrv.ctx = ctx
            threading.Thread(target=tsrv.serve_forever, daemon=True).start()
            print("  · HTTPS 도 엽니다: https://%s:%d/  (인증서에 적힌 이름으로 접속 — 팝업 알림 가능)" % (socket.getfqdn(), TLS_PORT))
            print("    방화벽 1회: netsh advfirewall firewall add rule name=\"SD Intake TLS %d\" dir=in action=allow protocol=TCP localport=%d"
                  % (TLS_PORT, TLS_PORT))
        else:
            print("  · HTTPS 꺼짐 (인증서 없음: %s) — http 만 동작, 팝업 알림은 탭 제목 표시로 대체됩니다." % TLS_CERT)
    except Exception as e:
        print("  [!] HTTPS 를 열지 못했습니다(http 는 정상): %s" % e)

    # 포트를 잡은 뒤에 연다 — 먼저 열면 브라우저가 연결 실패를 띄운다.
    # 자동시작(시작프로그램)에서는 매 부팅마다 탭이 뜨지 않도록 --noopen 으로 부른다.
    if "--noopen" not in sys.argv:
        local = "http://127.0.0.1:%d/" % PORT
        print("  · 브라우저에서 %s 를 엽니다. (자동 열기를 끄려면 --noopen)" % local)
        print("")
        threading.Timer(0.4, lambda: webbrowser.open(local)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
