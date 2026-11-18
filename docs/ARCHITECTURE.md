# 아키텍처 (Architecture)

## 구성요소와 포트

| 구성요소 | 파일 | 포트 · 바인딩 | 역할 |
|---|---|---|---|
| 접수 창구 | `ops/task-dashboard/chat-ui/chat-server.py` (+ `accounts.py`, `chat-page.html`) | 운영 5179 · `0.0.0.0`(사내망) / 시험 · 데모 5183 · 데모는 `127.0.0.1` / HTTPS 5180(인증서 있을 때) | 사번 로그인 · 세션 쿠키(`HttpOnly; SameSite=Strict`) · CSRF · 5분 멱등성 · 선택 PIN. 업무 규칙은 `intake-server.py` 를 **모듈로 불러** 그 Handler 를 이어받는다 |
| 접수 규칙 코어 | `ops/task-dashboard/intake-server.py` | (단독 실행도 가능 · 5179) | 새 문의 · 추가 질문 · 종결, 요청 제한, 첨부 검증, 공개 화면 정리(`_public_view`) |
| 문의 로그 기록기 | `ops/task-dashboard/log-inquiry.py` | — | `inquiry-log.js` 의 **유일한 writer**(append-only · id 병합) |
| 문의 데스크 + 허브 | `ops/task-dashboard/desk/desk-server.py` (+ `desk.html`) | 5177 / 시험 · 데모 5187 · **`127.0.0.1` 전용** | 봇 답 판정 · 담당자 답변(둘 다 `log-inquiry.py` 경유), 현황 · 지식 공백 · 장부 요약. 허브 경로 `/board` `/archive` `/erd` `/metrics` `/bot-metrics` 는 GET 읽기 전용 |
| 업무 보드 | `ops/task-dashboard/task-board.html` | 파일로 열기 | 카드(`TASKS` 배열) · 정기업무 · 문의 처리 이력. 허브(`/board`)로 열면 「보기 전용」 띠 + 저장 차단 |
| 보드 저장 서버 | `ops/task-dashboard/board-saver.py` | 5178 · **`127.0.0.1` 전용** | 보드 💾저장을 원본 파일에 원자적으로 덮어쓰기. Origin `null` 만 + 쓰기 토큰 `_saver_token.js` · 형식 검증 · 백업 |
| 트리니티봇 | `ops/task-dashboard/vacation/` | — (기본 꺼짐) | `watcher.py` 감시자 → `engines.py` 로 Claude Code CLI 읽기 전용 호출 → `apply_answer.py` 쓰기 게이트 → `ledger.py` 검증 장부. `mode.py` 켜기/끄기/보고서, `servers.py` 서버 지킴이, `prefetch.py` 선행 검색 |
| 지식 아카이브 | `ops/build_archive.py` · `ops/articles/**.md` · `ops/templates/archive_shell.html` | 파일 | 마크다운 정본 → 단일 `archive.html` + `archive-structure.md`. 빌드 때 다크/라이트 대비 색표 생성 |
| 관계도/온톨로지 | `ops/erd-graph/` | 파일 | `schema_ontology.json` → `erd-viewer.html`(`viewer_builder.py`), 질의 `query_ontology.py` · `ontology_engine.py`, 동기화 `sync_erd_ontology.py`(DDIC 추출본이 있을 때) |
| 지표 | `ops/task-dashboard/metrics.html` · `bot-metrics.html` | 파일 / 허브 | 협업 지표(문의 로그 집계) · 봇 지표(장부가 만든 데이터 파일) |
| 공통 디자인 | `ops/templates/ops-console.md` | — | `--oc-*` 색 토큰 · 위쪽 메뉴 규격(담당자 화면 6종 공통, 요청자 창구에는 넣지 않음) |

## 데이터 파일 — 누가 쓰나

| 데이터 | 위치(데모) | 쓰는 쪽 | 읽는 쪽 |
|---|---|---|---|
| 문의 로그 `inquiry-log.js` | `.runtime/demo/` (운영: `ops/task-dashboard/`) | `log-inquiry.py` 만 — 접수 창구(새 문의 · 추가 질문 · 종결), 데스크(판정 · 답변), 쓰기 게이트(AI 답) | 창구 · 데스크 · 보드 · 지표 · 봇 · 장부 |
| 프로파일 `profiles.json` | `.runtime/_intake-profiles-demo/` (운영: `.runtime/_intake-profiles/`) | `accounts.py`(파일 락 · 백업) | 접수 창구 |
| 첨부 사진 | `.runtime/_intake-attachments*/` | 접수 코어(새 문의 · 추가 질문에 실려서만), 종결 시 자동 삭제 | 창구(본인만) · 봇(Read) |
| 봇 런타임(장부 journal · 하트비트 · 실행 기록 · 지표 데이터) | `.runtime/_vacation-runtime/` | 감시자 · 쓰기 게이트 · 장부 | 데스크 · 봇 지표 · 복귀 보고서 |
| 보드 `task-board.html` | `ops/task-dashboard/` | 브라우저(로컬 저장) · `board-saver.py` | 사람 · 허브(읽기 전용) |
| 아카이브 정본 `articles/**.md` | `ops/articles/` | 사람 | `build_archive.py` · 봇(읽기) · 데스크 |
| 온톨로지 `schema_ontology.json` | `ops/erd-graph/` | `sync_erd_ontology.py` | 뷰어 · 질의 · 데스크 `/api/ontology` |

`.runtime/` 은 git 에 올리지 않습니다(`.gitignore`).

## 경계

```
 [요청자 브라우저] ──HTTP(사내망)──▶ chat-server (5179)  ── 쓰기 3종 ──▶ log-inquiry.py ──▶ inquiry-log.js
                                                                                  ▲
 [담당자 PC 브라우저] ──127.0.0.1──▶ desk-server (5177) ── 판정 · 답변 ───────────┤
                     ──file:// ────▶ task-board.html ──127.0.0.1──▶ board-saver (5178)
                                                                                  │
 [트리니티봇] Claude Code CLI (읽기 전용) ──JSON──▶ apply_answer.py (쓰기 게이트) ─┘
```

- 요청자 화면은 내부 화면(보드 · 로그 원본 · 아카이브)을 서빙하지 않습니다. 접수번호만으로 남의 문의를 보는 경로도 열지 않습니다.
- 콘솔 메뉴는 데스크(5177/5187)로 열면 허브 경로로, 파일로 열면 상대 경로로 이동합니다.
