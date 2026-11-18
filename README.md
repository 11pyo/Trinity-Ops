# Trinity-Ops — 상위 업무 프로시저(운영) 솔루션

> 기존에 따로 있던 구성요소(**지식 아카이브 · AI 1차 응답 봇 · 사번 로그인 접수 창구 · 담당자 문의 데스크 · 업무 보드 · 관계도/온톨로지**)를
> **하나의 업무 절차로 묶은 상위 업무 프로시저(운영) 솔루션**입니다.

ERP(SAP SD) 운영 지원 데스크에서 실제로 쓰던 통합 운영 콘솔을 **익명화한 공개판**입니다.
회사 · 사람 · 사내 시스템 식별자와 실데이터는 모두 빠져 있고, 동작 확인용 **합성 표본(SAMPLE)** 만 들어 있습니다.

## 한 장 요약 — 절차

```
 접수(사번 로그인 대화방)
   └→ AI 1차 응답(트리니티봇 · 항상 「AI」 표기 · 쓰기 권한 없음 → 쓰기 게이트가 기록)
        └→ 사람 검토 · 판정(맞음 / 보완 / 틀림)  ← 문의 데스크
             └→ 검증 장부(판정 통계가 기준을 넘겨야만 봇 권한 확대 — 결정은 사람)
                  └→ 지식 박제(아카이브 아티클 · 관계도/온톨로지)
                       └→ 업무 카드 갱신(업무 보드)
```

자세한 절차와 안전 불변식은 [`docs/PROCEDURE.md`](docs/PROCEDURE.md), 구성요소 · 포트 · 데이터 파일 · 누가 무엇을 쓰는지는 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

지식 층(아카이브)의 설계 청사진은 자매 공개 프로젝트 **Third-Party-Brain** — https://github.com/11pyo/Third-Party-Brain 에 있습니다.

## 빠른 시작 (데모)

Python 3.10+ (표준 라이브러리만으로 서버가 돕니다. 아카이브 빌드에만 `pip install pyyaml`).

```bash
# 1) 접수 창구 — 사번 로그인 대화방 (표본 사번: 90000001 / 90000002 / 90000003)
python ops/task-dashboard/chat-ui/chat-server.py --demo            # → http://127.0.0.1:5183/

# 2) 담당자 문의 데스크 + 허브(보드 · 아카이브 · ERD · 지표, 읽기 전용) — 127.0.0.1 전용
python ops/task-dashboard/desk/desk-server.py --demo               # → http://127.0.0.1:5187/

# 3) (선택) 아카이브 · ERD 뷰어 생성
python ops/build_archive.py                                         # → ops/archive.html
python ops/erd-graph/viewer_builder.py                              # → ops/erd-graph/erd-viewer.html

# 시험
python -B tests/smoke_test.py      # 저장소를 임시 폴더로 복사해 서버 · 쓰기 · 회귀 시험 (표본 파일은 안 바뀜)
python tools/leak_scan.py          # 공개 금지 식별자 검사 (걸리면 종료코드 1)
```

`--demo` 는 `sample-data/` 를 **`.runtime/demo/` 로 복사한 사본**에 씁니다. 처음 상태로 돌리려면 `.runtime/` 를 지우십시오.

### AI 1차 응답 봇(트리니티봇) — 기본 꺼짐

봇 파이프라인(`ops/task-dashboard/vacation/`: 감시자 → 읽기 전용 에이전트 → 쓰기 게이트 → 검증 장부)은 **Claude Code CLI(`claude`)** 가 있어야 돌며 **기본으로 꺼져 있습니다.**
켜려면 `python ops/task-dashboard/vacation/mode.py on`, 끄려면 `off`. 시험(`smoke_test.py`)은 봇을 절대 부르지 않습니다.
봇 규칙은 [`ops/task-dashboard/bot-operating-rules.md`](ops/task-dashboard/bot-operating-rules.md) — 회사별 내용(연락처 · 셀프서비스 경로)을 채워 쓰십시오.

## 폴더

| 경로 | 내용 |
|---|---|
| `ops/task-dashboard/chat-ui/` | 접수 창구(사번 로그인 · 문의 1건 = 대화방 1개) |
| `ops/task-dashboard/desk/` | 담당자 문의 데스크 + 읽기 전용 허브 |
| `ops/task-dashboard/intake-server.py` | 접수 업무 규칙 코어(창구가 모듈로 불러 씀) |
| `ops/task-dashboard/log-inquiry.py` | 문의 로그(`inquiry-log.js`)의 **유일한 기록 경로**(append-only) |
| `ops/task-dashboard/vacation/` | 트리니티봇(감시자 · 엔진 · 쓰기 게이트 · 검증 장부) |
| `ops/task-dashboard/board-saver.py` · `task-board.html` | 업무 보드와 로컬 저장 서버(127.0.0.1 · 출처 + 토큰) |
| `ops/build_archive.py` · `ops/articles/` · `ops/templates/` | 지식 아카이브(마크다운 정본 → 단일 HTML, 빌드 때 다크/라이트 대비 테마) |
| `ops/erd-graph/` | 테이블 · 프로그램 · T-Code 관계도/온톨로지 뷰어 |
| `sample-data/` | 합성 표본 데이터 |
| `tests/` · `tools/` | 스모크 시험 · 유출 검사기 |

---

## English

**Trinity-Ops** is an *upper-layer business-procedure (operations) solution*: it takes components that already existed separately — a **knowledge archive**, an **AI first-responder bot**, an **employee-ID login intake chat**, a **staff inquiry desk**, a **task board**, and a **table/program ontology viewer** — and binds them into **one operating procedure**:

1. **Intake** — requesters log in with an employee ID; one inquiry = one chat room.
2. **AI first response** — the bot drafts an answer that is **always labelled as AI**. The bot has **no write permission**; a separate *write gate* validates and records its output.
3. **Human review / grading** — staff grade every AI answer (correct / needs-fix / wrong) at the desk.
4. **Verification ledger** — grading statistics gate any expansion of the bot's authority (a human still decides).
5. **Knowledge capture** — confirmed facts go into the archive (and the ontology).
6. **Task card update** — follow-up work lands on the task board.

This repository is an **anonymized public copy** of a private system used by an ERP (SAP SD) support desk. All company, person and internal-system identifiers and all real data were removed; only clearly marked synthetic **SAMPLE** data ships. The knowledge-layer blueprint lives in the sibling project **Third-Party-Brain** (https://github.com/11pyo/Third-Party-Brain).

Quick start: `python ops/task-dashboard/chat-ui/chat-server.py --demo` (sample IDs `90000001`–`90000003`) and `python ops/task-dashboard/desk/desk-server.py --demo`; run `python -B tests/smoke_test.py` and `python tools/leak_scan.py`. The bot pipeline needs the Claude Code CLI and is **off by default**. UI text and comments are mostly Korean.

Safety invariants: write servers bind to localhost only; the public intake exposes exactly three write endpoints (new / follow-up / close); the AI label is mandatory; archive anchors are internal and never shown to requesters; the bot cannot write — the gate does. See `docs/PROCEDURE.md` and `docs/ARCHITECTURE.md`.

License: MIT — Copyright (c) 2026 11pyo.
