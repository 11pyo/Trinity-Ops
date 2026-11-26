# -*- coding: utf-8 -*-
"""
engines.py — 트리니티봇을 **어느 LLM CLI 로도** 부를 수 있게 하는 얇은 어댑터 층 (2026-09-18 신설, DEV-047)

왜 만드나
  지금까지 운영 체계는 Claude 하나를 전제로 만들어졌다. 호출 명령·출력 형식·도구 이름·격리 방식이
  전부 Claude Code 에 묶여 있어, 다른 모델로 바꾸려면 코드를 고쳐야 했다. 이 파일이 그 묶임을 한 곳에 모은다.

⛔ 제1 원칙 — **claude 어댑터는 오늘과 바이트 동일해야 한다.**
  엔진 독립화의 유일한 제약은 「기존 성능이 낮아지거나 깨지지 않을 것」이다. 그래서 claude 경로의
  실행 인자·환경변수·출력 파싱은 이 파일로 옮기기만 하고 한 글자도 바꾸지 않았다.
  골든 시험(`unit_golden.py`)이 argv 와 프롬프트 골격을 바이트로 대조해 이 약속을 지킨다.

⚠️ 제2 원칙 — **엔진을 바꾸면 안전 경계도 바뀐다.**
  트리니티봇의 셸·파일쓰기·하위 에이전트·개발계100 차단은 Claude 의 「거부가 허용을 이긴다」 규칙에 기대고 있다.
  다른 엔진은 허용목록·승인모드 등 다른 방식을 쓴다. 그래서 어댑터마다 `isolation` 을 선언하고,
  **`verified` 가 참인 엔진만** 무인 실행에 쓸 수 있게 한다(실측 증명 전에는 기동 거부 — `check_usable()`).
  증명 절차는 `probe_isolation.py`(차단 도구를 실제로 시켜 보고 거부되는지 확인)가 맡는다.

구조
  build_invocation(engine, ctx)  → {"argv": [...], "env": {...}, "notes": [...]}   실행 직전 준비(설정 파일 쓰기 포함)
  parse_result(engine, raw)      → 정규화된 결과 dict (claude stream-json 의 result 줄과 **같은 모양**)
  parse_behavior(engine, lines)  → 행동 기록 dict (watcher.behavior_of 와 같은 모양)
  check_usable(engine)           → (쓸 수 있나, 사유)
  도구 이름은 엔진마다 다르다 — `TOOLS` 의 논리 이름(read/grep/glob/web_search/web_fetch/dev120_*)으로 적고
  어댑터가 그 엔진의 실제 이름으로 바꾼다.
"""

import io
import json
import os
import re
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 논리 도구 이름 → 엔진별 실제 이름
#    트리니티봇에게 허용하는 것은 「읽기 · 개발계120 조회 · 제한된 웹 조사」뿐이다(운영규칙 §0·§6).
LOGICAL_TOOLS = ["read", "grep", "glob",
                 "dev120_searchObject", "dev120_getObjectSource", "dev120_objectStructure",
                 "dev120_mainPrograms", "dev120_usageReferences", "dev120_findDefinition",
                 "web_search", "web_fetch"]

_CLAUDE_NAMES = {
    "read": "Read", "grep": "Grep", "glob": "Glob",
    "dev120_searchObject": "mcp__mcp-abap-dev120__searchObject",
    "dev120_getObjectSource": "mcp__mcp-abap-dev120__getObjectSource",
    "dev120_objectStructure": "mcp__mcp-abap-dev120__objectStructure",
    "dev120_mainPrograms": "mcp__mcp-abap-dev120__mainPrograms",
    "dev120_usageReferences": "mcp__mcp-abap-dev120__usageReferences",
    "dev120_findDefinition": "mcp__mcp-abap-dev120__findDefinition",
    "web_search": "WebSearch", "web_fetch": "WebFetch",
}

# Gemini CLI(공식 · npm @google/gemini-cli 0.60) 내장 도구 이름 — 2026-09-22 실측(stream-json tool_use 의 tool_name).
# 허용 범위는 이 표가 아니라 정책 파일 `policies/gemini-trinitybot.toml`(기본 거부 + 읽기·찾기·웹만 허용)이 강제한다.
_GEMINI_NAMES = {
    "read": "read_file", "grep": "grep_search", "glob": "glob",
    "web_search": "google_web_search", "web_fetch": "web_fetch",
    # MCP 도구는 서버 단위로 허용한다(--allowed-mcp-server-names) — 개별 이름 매핑 불필요
}


def tool_names(engine, logical=None):
    """논리 도구 이름 목록을 그 엔진의 실제 이름으로 바꾼다(매핑에 없으면 조용히 건너뛴다)."""
    names = LOGICAL_TOOLS if logical is None else logical
    table = {"claude": _CLAUDE_NAMES, "gemini": _GEMINI_NAMES}.get(engine, {})
    out = []
    for n in names:
        real = table.get(n)
        if real and real not in out:
            out.append(real)
    return out


# ── 엔진 선언 ────────────────────────────────────────────────────────────
#  isolation: 이 엔진에서 트리니티봇 격리(셸·쓰기·하위 에이전트·개발계100 차단)가 **무엇으로** 담보되나
#  verified : 그 차단이 실제로 먹는 것을 실측으로 확인했나 (거짓이면 무인 실행 거부)
ENGINES = {
    "claude": {
        "cmd_env": ("APPDATA", "npm", "claude.cmd"),
        "isolation": "전용 설정 파일(deny 목록) — 막는 규칙이 어느 층의 허용보다 우선. --strict-mcp-config 로 연결도 하나만",
        "verified": True,          # 2026-09-11 실측: Bash 시도가 「No such tool available」로 거부됨(행동 로그)
        "web": True,
    },
    "gemini": {
        # 공식 Gemini CLI + API 키(GEMINI_API_KEY). 2026-09-22 agy(안티그래비티)에서 교체 —
        # agy 는 권한 확인 전부 생략(--dangerously-skip-permissions) 없이는 헤드리스로 답을 못 냈다(=격리 0).
        "cmd_env": ("APPDATA", "npm", "gemini.cmd"),
        "isolation": "관리자 등급 정책(--admin-policy: 기본 거부 + 읽기·찾기·웹만 허용) + 전용 설정 폴더(GEMINI_CLI_HOME) — 개인 확장·MCP·메모리 미적재",
        # ✅ 2026-09-22 담당자 지시로 켬(「클로드 토큰 없을 시 제미나이로 대체 모드 켜 둬라」 — 사람이 켠다는 INV13 충족).
        #    근거: 격리 실측 전 실행 셸·쓰기 흔적 0 · 3.8-flash 통과 3회(나머지는 API 한도 소진으로 판정 불가) ·
        #    3.1-flash-lite 3회 연속 통과. 격리는 모델이 아니라 CLI 정책(--admin-policy)이 강제한다.
        # ⛔ 2026-11-26 담당자 지시로 비활성화: gemini-3.8-flash 페일오버 금지 → agy 수소(Hydrogen) 모델 단독 사용.
        "verified": False,
        "web": True,
        "key_env": "GEMINI_API_KEY",
        # 모델 고정 — 「자동」은 가벼운 모델과 오가며 결과가 흔들렸다(2026-09-22 시험 1회 읽기 누락).
        # config.json 의 gemini_model 로 바꿀 수 있다. 바꾸면 probe_isolation.py 를 다시 돌린다.
        "model": "gemini-3.8-flash",
        # 무료 등급은 한도가 **모델마다 따로** 잡힌다 — 앞 모델이 소진되면 다음 모델로(결제 없이 버티는 방법).
        # 사슬에 넣는 모델은 전부 probe_isolation.py --model <이름> 3회 연속 통과가 먼저다.
        #   2026-09-22: 3.8-flash 단독. flash-lite 는 격리 3/3 통과했지만 **답 품질 시험에서 탈락**(담당자 선택으로 제외):
        #   실제 트리니티봇 프롬프트 재실행 — 선례 있는 문의는 선례를 그대로 복사, 새 판단이 필요한 개발 요청은
        #   「기존 화면으로 이미 된다」를 놓쳐 불필요한 개발로 안내 + 현업 답에 테이블·필드명 노출.
        #   3.8 한도가 차면 대체는 실패로 기록되고 트리니티봇은 「접수 확인」만 남긴다(틀린 답보다 낫다).
        "models": ["gemini-3.8-flash"],
        "policy": ("policies", "gemini-trinitybot.toml"),
    },
    "agy": {
        # Antigravity CLI (2026-11-20): toolPermission=request-review + permissions.deny 로 격리 보장.
        # --dangerously-skip-permissions 는 절대 넘기지 않으며, probe_isolation.py 3/3 통과 후 verified=True.
        "cmd_env": ("LOCALAPPDATA", "agy", "bin", "agy.exe"),
        "isolation": "전용 설정 파일(--settings: toolPermission=request-review + permissions.deny 로 셸·쓰기·브라우저·DEV 100 차단, 읽기·웹·DEV 120만 허용)",
        "verified": True,  # 2026-11-20 실측: probe_isolation.py agy 3/3 연속 통과(42/44/35초)
        "web": True,
        "effort": "medium",
    },
}

DEFAULT_ENGINE = "claude"


def resolve(cfg=None):
    """쓸 엔진 이름. 우선순위: 환경변수 > config.json > 기본값(claude)."""
    v = (os.environ.get("TRINITYBOT_ENGINE") or "").strip()
    if not v:
        v = ((cfg or {}).get("engine") or "").strip()
    return v or DEFAULT_ENGINE


def engine_cmd(engine):
    spec = ENGINES.get(engine) or {}
    parts = spec.get("cmd_env")
    if not parts:
        return ""
    return os.path.join(os.environ.get(parts[0], ""), *parts[1:])


def check_usable(engine):
    """(쓸 수 있나, 사유) — 무인 실행 직전 관문.

    ⚠️ 격리가 실측으로 증명되지 않은 엔진은 **거부한다.** 부재중에는 틀린 답을 잡아줄 사람이 없고,
    셸·쓰기가 열린 채로 도는 것은 답이 틀리는 것보다 나쁘다. 실측은 probe_isolation.py 가 한다.
    """
    spec = ENGINES.get(engine)
    if not spec:
        return False, "모르는 엔진입니다: %s (아는 엔진: %s)" % (engine, ", ".join(sorted(ENGINES)))
    cmd = engine_cmd(engine)
    if not cmd or not os.path.exists(cmd):
        return False, "%s 실행 파일을 찾지 못했습니다: %s" % (engine, cmd)
    if spec.get("key_env") and not os.environ.get(spec["key_env"]):
        return False, "%s 인증 키 환경변수(%s)가 비어 있습니다" % (engine, spec["key_env"])
    if spec.get("policy") and not os.path.exists(os.path.join(HERE, *spec["policy"])):
        return False, "%s 격리 정책 파일이 없습니다: %s" % (engine, os.path.join(*spec["policy"]))
    if not spec.get("verified"):
        return False, ("%s 는 격리(셸·쓰기·개발계100 차단)가 아직 실측으로 증명되지 않았습니다 — "
                       "probe_isolation.py 로 확인한 뒤 engines.py 의 verified 를 켜십시오" % engine)
    return True, ""


_AGY_MODEL_CACHE = {"ts": 0.0, "lines": None}
_AGY_LIGHT_TIERS = {"hydrogen": 0}  # 2026-11-26 담당자 지시: agy 페일오버는 수소(Hydrogen) 모델만 허용(flash 계열 제외)
_AGY_FALLBACK_MODELS = ["gemini-4-hydrogen-medium", "gemini-4-hydrogen-low"]


def _parse_agy_model_key(mid, preferred_effort="medium"):
    """경량 Gemini 모델만 점수를 매긴다. (티어순위, -세대튜플..., effort순위) — 작을수록 우선."""
    m = re.match(r"^gemini-(\d+(?:\.\d+)*)-([a-z0-9-]+?)(?:-(low|medium|high|xhigh))?$", mid)
    if not m:
        return None
    ver_s, tier, eff = m.group(1), m.group(2), m.group(3) or "medium"
    if tier not in _AGY_LIGHT_TIERS or eff == "xhigh":
        return None
    ver = (tuple(int(x) for x in ver_s.split(".")) + (0, 0, 0))[:3]
    eff_order = {"medium": 0, "low": 1, "high": 2} if preferred_effort == "medium" else (
        {"low": 0, "medium": 1, "high": 2} if preferred_effort == "low" else {"high": 0, "medium": 1, "low": 2}
    )
    return (tuple(-v for v in ver), _AGY_LIGHT_TIERS[tier], eff_order.get(eff, 9), mid)


def select_agy_models(effort="medium", lines=None, cmd=None, limit=3):
    """`agy models` 출력에서 최신 세대 경량 Gemini 모델(기본 effort=medium)을 동적으로 고른다."""
    raw_lines = lines
    if raw_lines is None:
        now = time.time()
        if _AGY_MODEL_CACHE["lines"] is not None and (now - _AGY_MODEL_CACHE["ts"]) < 3600:
            raw_lines = _AGY_MODEL_CACHE["lines"]
        else:
            c = cmd or engine_cmd("agy")
            if c and os.path.exists(c):
                try:
                    p = subprocess.run([c, "models"], capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=15)
                    if p.returncode == 0 and (p.stdout or "").strip():
                        raw_lines = p.stdout.splitlines()
                        _AGY_MODEL_CACHE["lines"] = raw_lines
                        _AGY_MODEL_CACHE["ts"] = now
                except Exception:  # noqa: BLE001
                    raw_lines = None
    scored = []
    seen = set()
    for ln in (raw_lines or []):
        tok = (ln or "").strip().split()
        if not tok:
            continue
        mid = tok[0]
        if mid in seen:
            continue
        key = _parse_agy_model_key(mid, preferred_effort=(effort or "medium").lower())
        if key is not None:
            seen.add(mid)
            scored.append((key, mid))
    scored.sort(key=lambda x: x[0])
    out = [mid for _, mid in scored[:limit]] if limit else [mid for _, mid in scored]
    return out or list(_AGY_FALLBACK_MODELS)


def format_stdin(engine, prompt):
    """엔진별 표준입력 형식으로 변환한다(agy 는 stream-json user 이벤트, 나머지는 원문 그대로)."""
    if engine == "agy":
        return json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
    return prompt


# ── 실행 준비 ────────────────────────────────────────────────────────────
# 사람 세션(Claude 데스크톱 앱 · Claude Code)이 자식 프로세스에 심어 주는 환경 변수 — 트리니티봇은 그 세션과 무관하게 돌아야 한다.
# 감시자를 AI 세션에서 띄우면(`mode.py on` 등) 이 변수들이 새어 들어가 `claude -p` 가 인증 갱신을 그 세션(호스트)에 위임하려다
# 「Failed to authenticate: OAuth session expired and could not be refreshed」 로 실패한다 — 2026-10-07 09:26·09:36 베타 첫 문의가
# 답을 못 받았다(접수 확인만 나가고 3회 실패). 장기 토큰(`claude setup-token` → CLAUDE_CODE_OAUTH_TOKEN)은 무인 실행의 정식 인증이라 남긴다.
_HOST_ENV_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK", "CLAUDE_PREVIEW", "CLAUDE_PID")
_HOST_ENV_NAMES = ("CLAUDECODE", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
_KEEP_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_GIT_BASH_PATH")


def strip_host_env(env):
    """env(dict)에서 사람 세션 전용 변수를 제거해 돌려준다(제자리 수정). 장기 토큰·Git Bash 경로는 남긴다."""
    for k in list(env):
        if k in _KEEP_ENV:
            continue
        if k in _HOST_ENV_NAMES or k.startswith(_HOST_ENV_PREFIXES):
            env.pop(k, None)
    return env


def build_invocation(engine, ctx):
    """실행 인자·환경변수를 만든다. 설정 파일이 필요한 엔진은 여기서 쓴다.

    ctx = {rt, root, mcp_config, settings_obj, extra_dirs, allowed(논리 도구 목록, 선택)}
    """
    if engine == "claude":
        return _claude_invocation(ctx)
    if engine == "gemini":
        return _gemini_invocation(ctx)
    if engine == "agy":
        return _agy_invocation(ctx)
    raise ValueError("모르는 엔진: %s" % engine)


def _claude_invocation(ctx):
    """⛔ 오늘 watcher.call_claude 가 만들던 것과 **바이트 동일**하게 유지한다(골든 시험이 대조)."""
    rt = ctx["rt"]
    notes = []
    # 실행 파일 경로는 **주입을 허용한다** — 시험이 가짜 CLI 로 바꿔 끼우고(2026-09-18 시험이 이 누락을 잡았다),
    # 설치 위치가 다른 PC 도 설정으로 가리킬 수 있게. 주지 않으면 기본 위치를 쓴다.
    cmd0 = ctx.get("cmd") or engine_cmd("claude")
    sp = os.path.join(rt, "trinitybot-settings.json")
    try:
        # 매 호출 정해진 내용으로 다시 쓴다 — 누가 지우거나 고쳐도 되돌아온다
        io.open(sp, "w", encoding="utf-8").write(json.dumps(ctx["settings_obj"], ensure_ascii=False, indent=2))
    except OSError:
        sp = ""
        notes.append("전용 설정 파일을 쓰지 못했습니다 — 기본 권한으로 돕니다")
    mcp_cfg = ctx.get("mcp_config") or ""
    argv = [cmd0, "-p", "--permission-mode", "dontAsk",
            "--output-format", "stream-json", "--verbose", "--setting-sources", "project,local"] + \
        (["--settings", sp] if sp else []) + \
        (["--strict-mcp-config", "--mcp-config", mcp_cfg] if mcp_cfg and os.path.exists(mcp_cfg) else []) + \
        (["--model", ctx["model"]] if ctx.get("model") else []) + \
        ["--allowedTools"] + tool_names("claude", ctx.get("allowed"))
    # ↑ --model 은 **줄 때만**(성능 비교 시험용 · 2026-09-22). 감시자는 주지 않으므로 운영 인자는 골든 그대로.
    for d in ctx.get("extra_dirs") or ():
        if d and os.path.isdir(d):
            argv += ["--add-dir", d]
    env = strip_host_env(dict(os.environ))
    env["PYTHONIOENCODING"] = "utf-8"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_OMC"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    # 개발계 MCP 서버 연결 대기 — 기본 30초. 서버를 구글 드라이브(G:)에서 실행하면 시작에 15~32초가 걸려 연결이 실패했다(2026-10-06).
    # 지금은 로컬 미러(C:)에서 실행해 1~2초지만, 드라이브가 느려지는 날을 대비한 여유다. 사용자가 이미 정해 둔 값은 존중한다.
    env.setdefault("MCP_TIMEOUT", "90000")
    return {"argv": argv, "env": env, "notes": notes}


def _gemini_invocation(ctx):
    """공식 Gemini CLI — Claude 경로와 같은 격리를 **정책 엔진**으로 만든다 (2026-09-22 실측).

    · `--admin-policy` : 관리자 등급(최상위)이라 개인·작업공간 정책이 뒤집지 못한다. 기본 거부 + 읽기·찾기·웹만 허용.
      거부된 도구는 모델에게 **아예 제공되지 않는다**(실측: 셸·쓰기를 시켜도 호출 시도 0).
    · `GEMINI_CLI_HOME` : 호출 전용 설정 폴더 — 담당자 개인 확장·MCP·메모리·정책이 섞이지 않는다
      (Claude 쪽 `--setting-sources project,local` 과 같은 역할).
    · 인증은 API 키(GEMINI_API_KEY). GOOGLE_API_KEY 가 함께 있으면 CLI 가 그쪽을 먼저 쓰므로 자식 환경에서 뺀다.
    · `--skip-trust` : 헤드리스는 신뢰 폴더 확인에서 멈춘다. 작업공간 정책은 CLI 가 원래 끄고 있고,
      이 폴더엔 `.gemini/` 작업공간 설정이 없다.
    """
    rt = ctx["rt"]
    notes = []
    home = ctx.get("gemini_home") or os.path.join(rt, "gemini-home")
    try:
        os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
        io.open(os.path.join(home, ".gemini", "settings.json"), "w", encoding="utf-8").write(json.dumps(
            {"security": {"auth": {"selectedType": "gemini-api-key"}},
             "general": {"disableAutoUpdate": True}}, ensure_ascii=False, indent=2))
    except OSError as e:
        notes.append("Gemini 전용 설정 폴더를 쓰지 못했습니다: %s" % e)
    pol = os.path.join(HERE, *ENGINES["gemini"]["policy"])
    argv = [ctx.get("cmd") or engine_cmd("gemini"), "-p", " ", "--skip-trust",
            "-o", "stream-json", "--admin-policy", pol]
    model = ctx.get("model") or ENGINES["gemini"].get("model")
    if model:
        argv += ["-m", model]
    for d in ctx.get("extra_dirs") or ():
        if d and os.path.isdir(d):
            argv += ["--include-directories", d]
    env = dict(os.environ)
    env.pop("GOOGLE_API_KEY", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["GEMINI_CLI_HOME"] = home
    env["GEMINI_DEFAULT_AUTH_TYPE"] = "gemini-api-key"
    env["NO_COLOR"] = "1"
    return {"argv": argv, "env": env, "notes": notes}


def _agy_invocation(ctx):
    """Antigravity CLI(agy) — 통합 권한 엔진(toolPermission=request-review + permissions.deny)으로 격리한다."""
    rt = ctx["rt"]
    notes = []
    sp = os.path.join(rt, "agy-trinitybot-settings.json")
    settings = {
        "toolPermission": "request-review",
        "permissions": {
            "deny": [
                "command(*)",
                "unsandboxed(*)",
                "write_file(*)",
                "execute_url(*)",
                "download_url(*)",
                "mcp(mcp-abap-abap-adt-api/*)",
                "mcp(chrome-devtools/*)",
            ],
            "allow": [
                "read_file(*)",
                "read_url(*)",
                "mcp(mcp-abap-dev120/*)",
            ],
        },
    }
    try:
        io.open(sp, "w", encoding="utf-8").write(json.dumps(settings, ensure_ascii=False, indent=2))
    except OSError as e:
        notes.append("agy 전용 설정 파일을 쓰지 못했습니다: %s" % e)
        sp = ""
    argv = [ctx.get("cmd") or engine_cmd("agy")]
    if sp:
        argv += ["--settings", sp]
    argv += ["--disable-slash-commands", "--input-format", "stream-json", "--output-format", "stream-json"]
    model = ctx.get("model") or select_agy_models(effort=ctx.get("effort") or ENGINES["agy"].get("effort", "medium"))[0]
    if model:
        argv += ["--model", model]
    for d in ctx.get("extra_dirs") or ():
        if d and os.path.isdir(d):
            argv += ["--add-dir", d]
    env = strip_host_env(dict(os.environ))
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    return {"argv": argv, "env": env, "notes": notes}


# ── 출력 해석 ────────────────────────────────────────────────────────────
def parse_result(engine, raw):
    """엔진 출력에서 최종 결과를 꺼내 **claude stream-json 의 result 줄과 같은 모양**으로 돌려준다.

    같은 모양으로 맞추는 이유: 감시자(cycle)·행동 기록·복귀 보고서가 이미 그 모양을 읽고 있다.
    엔진을 바꿔도 그 아래는 손대지 않게 하려는 것이다.
    """
    lines = (raw or "").splitlines()
    if engine == "claude":
        return _last_json(lines, lambda e: e.get("type") == "result")
    if engine == "gemini":
        # 공식 CLI stream-json: 답은 assistant message 조각(delta)들을 이어 붙인 것, 끝에 result(통계) 한 줄.
        parts, res, denials = [], None, []
        for e in _json_lines(lines):
            t = e.get("type")
            if t == "message" and e.get("role") == "assistant":
                parts.append(str(e.get("content") or ""))
            elif t == "result":
                res = e
            elif t == "tool_result" and e.get("status") not in (None, "success"):
                er = e.get("error")
                denials.append({"tool_id": e.get("tool_id"), "status": e.get("status"),
                                "error": str(er.get("message") if isinstance(er, dict) else (er or ""))[:200]})
        if res is None and not parts:
            return None
        stats = (res or {}).get("stats") or {}
        return {"type": "result", "result": "".join(parts),
                "is_error": bool(res) and res.get("status") not in (None, "success"),
                "num_turns": stats.get("tool_calls"),
                "total_cost_usd": 0,           # API 는 토큰만 준다 — 금액 환산은 하지 않는다(요율이 모델마다 다름)
                "duration_ms": stats.get("duration_ms"),
                "usage": {"input_tokens": stats.get("input_tokens"), "output_tokens": stats.get("output_tokens"),
                          "cached": stats.get("cached")},
                "permission_denials": denials,
                "error": str(((res or {}).get("error") or {}).get("message")
                             if isinstance((res or {}).get("error"), dict) else ((res or {}).get("error") or ""))[:500]}
    if engine == "agy":
        res = None
        for e in _json_lines(lines):
            if e.get("event") == "result" and isinstance(e.get("result"), dict):
                res = e["result"]
        if res is None:
            return None
        status = res.get("status")
        usage = res.get("usage") or {}
        dur_s = float(res.get("duration_seconds") or 0)
        return {"type": "result", "result": str(res.get("response") or ""),
                "is_error": status != "SUCCESS",
                "num_turns": res.get("num_turns"),
                "total_cost_usd": 0,
                "duration_ms": round(dur_s * 1000),
                "usage": {"input_tokens": usage.get("input_tokens"),
                          "output_tokens": usage.get("output_tokens"),
                          "cached": usage.get("cache_read_tokens")},
                "permission_denials": [],
                "error": str(res.get("error") or "")[:500]}
    raise ValueError("모르는 엔진: %s" % engine)


def is_exhausted(engine, raw="", err="", d=None):
    """엔진이 한도 소진·Rate Limit·크레딧 부족·API 에러로 불능 상태인지 판별한다."""
    if engine == "claude":
        txt = (str(raw) or "") + " " + (str(err) or "") + " " + str((d or {}).get("result") or "")
        lower = txt.lower()
        if isinstance(d, dict) and d.get("is_error") and d.get("api_error_status"):
            return True
        exhausted_keywords = (
            "weekly limit", "rate limit", "rate_limit", "out_of_credits",
            "credit balance is too low", "exceeded your current quota",
            "overloaded", "too many requests", "hit your weekly limit", "hit your usage limit"
        )
        if any(kw in lower for kw in exhausted_keywords):
            return True
    if engine in ("gemini", "agy"):
        lower = ("%s %s %s %s" % (raw or "", err or "", (d or {}).get("result") or "", (d or {}).get("error") or "")).lower()
        kws = ("resource_exhausted", "quota", '"code":429', "status 429")
        if engine == "agy":
            kws = kws + ("rate limit", "rate_limit", "too many requests", "out of credits", "usage limit", "weekly limit", "exhausted")
        return any(k in lower for k in kws)
    return False


def _json_lines(lines):
    for line in lines:
        line = (line or "").strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            yield e


def model_of(engine, lines):
    """스트림에서 **실제로 답한 모델**의 이름 — 못 찾으면 빈 문자열.

    2026-09-30: 모델을 지정하지 않는 무인 호출에서는 CLI 버전이 권장 모델을 정한다(같은 날 2.1.280 = Opus 5.5, 2.1.285 = Sonnet 5.5).
    그래서 답마다 모델을 남긴다 — 이 흔적이 없으면 정확도가 어느 모델의 것인지 나중에 가를 수 없다.
    """
    init, used = "", []
    for e in _json_lines(list(lines)):
        t = e.get("type")
        if engine == "claude":
            if t == "system" and e.get("subtype") == "init" and e.get("model"):
                return str(e["model"])
        elif engine == "gemini":
            if t == "init" and e.get("model") and not init:
                init = str(e["model"])
            elif t == "result":
                used = sorted(((e.get("stats") or {}).get("models") or {}).keys())
        elif engine == "agy":
            if e.get("event") == "init":
                m = ((e.get("init") or {}).get("model") or "")
                if m:
                    return str(m)
    if engine == "gemini":
        return init if (init and init != "auto") else ("+".join(used) or init)
    return ""


def _last_json(lines, pred):
    found = None
    for line in lines:
        line = (line or "").strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and pred(e):
            found = e
    return found


def tool_events(engine, lines):
    """스트림에서 도구 사용 기록을 꺼낸다 → [(이름, 입력dict)] · 행동 기록(watcher.behavior_of)이 쓴다."""
    out = []
    for line in lines:
        line = (line or "").strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if engine == "claude":
            if e.get("type") == "assistant":
                for c in ((e.get("message") or {}).get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        out.append((str(c.get("name") or "?"), c.get("input") or {}))
        elif engine == "gemini":
            # 공식 CLI stream-json(2026-09-22 실측): {"type":"tool_use","tool_name":..,"parameters":{..}}
            if e.get("type") == "tool_use":
                out.append((str(e.get("tool_name") or "?"), e.get("parameters") or {}))
        elif engine == "agy":
            if e.get("event") == "step_update":
                su = e.get("step_update") or {}
                if su.get("step_type") == "tool" and su.get("state") == "DONE":
                    ti = su.get("tool_info") or {}
                    tname = str(su.get("tool_name") or ti.get("name") or "?")
                    params = ti.get("parameters") or {}
                    if tname == "call_mcp_tool" and params.get("ServerName") and params.get("ToolName"):
                        tname = "mcp__%s__%s" % (params.get("ServerName"), params.get("ToolName"))
                    out.append((tname, params))
    return out


_TO_CLAUDE_TOOL = {"read_file": "Read", "read_many_files": "Read", "grep_search": "Grep",
                   "search_file_content": "Grep", "glob": "Glob", "list_directory": "Glob",
                   "find_by_name": "Glob", "list_dir": "Glob",
                   "google_web_search": "WebSearch", "search_web": "WebSearch",
                   "web_fetch": "WebFetch", "read_url_content": "WebFetch"}


def _to_claude_input(name, p):
    if name == "Read":
        return {"file_path": p.get("file_path") or p.get("absolute_path") or p.get("Path") or ", ".join(p.get("paths") or [])}
    if name == "Grep":
        return {"pattern": p.get("pattern") or p.get("Query"), "path": p.get("dir_path") or p.get("path") or p.get("SearchPath"), "glob": p.get("include")}
    if name == "Glob":
        return {"pattern": p.get("pattern") or p.get("dir_path") or p.get("Pattern") or p.get("SearchDirectory") or p.get("DirectoryPath")}
    if name == "WebSearch":
        return {"query": p.get("query")}
    if name == "WebFetch":
        return {"url": p.get("url") or p.get("Url") or p.get("prompt")}
    return dict(p)


def claude_shaped_lines(engine, lines):
    """다른 엔진의 스트림을 **Claude stream-json 모양 줄**로 옮겨 적는다 → 행동 기록·웹 점검·복귀 보고서를 그대로 쓴다.

    도구 이름도 Claude 쪽 이름으로 바꾼다(read_file→Read 등). 모르는 도구는 원래 이름 그대로 — 점검에서 눈에 띄게.
    """
    if engine == "claude":
        return list(lines)
    out = []
    for name, p in tool_events(engine, lines):
        cn = _TO_CLAUDE_TOOL.get(name, name)
        out.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": cn, "input": _to_claude_input(cn, p or {})}]}}, ensure_ascii=False))
    d = parse_result(engine, "\n".join(lines))
    if d:
        out.append(json.dumps({"type": "result", "num_turns": d.get("num_turns"), "total_cost_usd": 0,
                               "duration_ms": d.get("duration_ms"),
                               "permission_denials": d.get("permission_denials") or []}, ensure_ascii=False))
    return out


def describe(engine):
    """제어판·보고서에 그대로 쓸 한 줄."""
    spec = ENGINES.get(engine) or {}
    return "%s — 격리: %s / 실측 증명: %s" % (
        engine, spec.get("isolation", "(선언 없음)"), "완료" if spec.get("verified") else "**아직 안 됨**")
