# -*- coding: utf-8 -*-
"""
answer_schema.py — 에이전트 답변(JSON)의 **규격과 정규화** (2026-09-18 신설 · DEV-047 4단계)

왜 필요한가
  트리니티봇의 출력은 JSON 하나다. Claude 는 지금 형식을 잘 지키지만, **모델이 바뀌면 모양이 조금씩 달라진다** —
  `answers` 를 빼고 배열만 주거나, 판정을 한국어로 쓰거나, 본문을 줄 배열로 주는 식이다.
  그때마다 파이프라인이 조용히 답을 흘리면 현업은 침묵을 본다. 그래서 **받아들일 수 있는 변형은 정규화하고,
  받아들일 수 없는 것은 시끄럽게 남긴다.**

설계 원칙
  · **관대하게 받고, 엄격하게 넘긴다.** 들어오는 모양은 여러 가지를 허용하되, 게이트로 넘기는 값은 한 가지로 고정.
  · **버리지 않는다.** 규격을 못 맞춘 답은 조용히 사라지지 않고 문제 목록에 남아 journal 로 간다
    (감시자가 「응답 누락」으로 재시도하거나 담당자 몫으로 남긴다 — 기존 동작과 같다).
  · **기존 동작을 깨지 않는다.** 지금 Claude 가 내는 정상 출력은 정규화를 거쳐도 **그대로**다(시험이 대조).
"""

import json
import os
import re

VERDICTS = ("answer", "hold", "escalate", "dev")

# 판정 값의 변형 — 모델마다 대소문자·한국어·서술형으로 낸다. 뜻이 분명한 것만 받는다.
_VERDICT_ALIAS = {
    "answer": "answer", "answered": "answer", "reply": "answer", "답변": "answer", "답": "answer",
    "hold": "hold", "pending": "hold", "wait": "hold", "대기": "hold", "보류": "hold",
    "escalate": "escalate", "escalation": "escalate", "에스컬레이션": "escalate", "이관": "escalate",
    "dev": "dev", "development": "dev", "개발": "dev", "개발요청": "dev",
}

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "트리니티봇 답변 규격",
    "type": "object",
    "required": ["answers"],
    "additionalProperties": False,
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "verdict", "public", "note"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": "^INQ-[0-9A-Za-z-]+$",
                           "description": "감시자가 넘겨준 문의 번호 — 목록에 없는 id 는 버려진다"},
                    "verdict": {"enum": list(VERDICTS),
                                "description": "answer=확정 답변 · dev=개발요청 평가 · escalate=직접 처리 불가 · hold=판단 유보"},
                    "public": {"type": "string",
                               "description": "현업이 읽을 본문. 표기·정정문구·시각은 게이트가 붙이므로 쓰지 않는다. 사내 식별자(카드·문의·SR 번호) 금지"},
                    "note": {"type": "string", "description": "담당자용 내부 메모 — 현업에게 나가지 않는다"},
                    "ref": {"type": "string", "description": "아카이브 앵커(#...) · 없으면 빈 문자열"},
                },
            },
        }
    },
}


def _text(v):
    """본문 값을 문자열로 — 모델이 줄 배열이나 숫자로 줄 때가 있다."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(_text(x) for x in v)
    if isinstance(v, dict):                      # {"text": "..."} 같은 포장
        for k in ("text", "content", "value"):
            if k in v:
                return _text(v[k])
        return json.dumps(v, ensure_ascii=False)
    return v if isinstance(v, str) else str(v)


def normalize(obj):
    """모델이 낸 것 → (정규화된 답 목록, 문제 목록).

    받아들이는 변형: 최상위가 배열 · `answers` 대신 `results`/`items` · 판정 한국어/대문자 ·
    본문이 줄 배열 · 키 앞뒤 공백. 받아들일 수 없는 것(문의 번호 없음 등)은 문제로 남긴다.
    """
    problems = []
    if obj is None:
        return [], ["답변 JSON 을 찾지 못했습니다"]
    if isinstance(obj, list):
        rows = obj
        problems.append("최상위가 배열이었습니다(answers 키 없음) — 그대로 받아 처리")
    elif isinstance(obj, dict):
        rows = None
        for k in ("answers", "results", "items", "data"):
            if isinstance(obj.get(k), list):
                rows = obj[k]
                if k != "answers":
                    problems.append("answers 대신 `%s` 키를 썼습니다 — 그대로 받아 처리" % k)
                break
        if rows is None:
            rows = [obj] if obj.get("id") else []
            if rows:
                problems.append("답이 배열이 아니라 단일 객체였습니다 — 1건으로 처리")
    else:
        return [], ["답변 JSON 의 최상위 형이 이상합니다: %s" % type(obj).__name__]

    out = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            problems.append("%d번째 답이 객체가 아닙니다(%s) — 버림" % (i + 1, type(r).__name__))
            continue
        r = {str(k).strip(): v for k, v in r.items()}
        iid = _text(r.get("id") or r.get("inquiry_id") or r.get("문의번호")).strip()
        if not iid:
            problems.append("%d번째 답에 문의 번호가 없습니다 — 버림" % (i + 1))
            continue
        raw_v = _text(r.get("verdict") or r.get("판정")).strip()
        v = _VERDICT_ALIAS.get(raw_v.lower())
        if v is None:
            v = "hold"
            problems.append("`%s` 의 판정 값 「%s」을 알 수 없어 hold 로 둡니다" % (iid, raw_v or "(없음)"))
        elif v != raw_v:
            problems.append("`%s` 의 판정 값 「%s」을 `%s` 로 맞췄습니다" % (iid, raw_v, v))
        extra = [k for k in r if k not in ("id", "verdict", "public", "note", "ref",
                                           "inquiry_id", "문의번호", "판정")]
        if extra:
            problems.append("`%s` 에 규격에 없는 키 %s — 무시합니다" % (iid, ", ".join(sorted(extra))))
        out.append({"id": iid, "verdict": v,
                    "public": _text(r.get("public") or r.get("답변")),
                    "note": _text(r.get("note") or r.get("메모")),
                    "ref": _text(r.get("ref")).strip()})
    return out, problems


def validate(answers):
    """정규화된 답이 규격을 지키는지 — 지금은 형·값만 본다(외부 라이브러리 없이)."""
    bad = []
    for a in answers or []:
        if not re.match(r"^INQ-[0-9A-Za-z-]+$", a.get("id", "")):
            bad.append("문의 번호 형식이 아닙니다: %r" % a.get("id"))
        if a.get("verdict") not in VERDICTS:
            bad.append("`%s` 판정 값이 규격 밖입니다: %r" % (a.get("id"), a.get("verdict")))
        for k in ("public", "note", "ref"):
            if not isinstance(a.get(k, ""), str):
                bad.append("`%s` 의 %s 가 문자열이 아닙니다" % (a.get("id"), k))
    return bad


def write_schema_file(path=None):
    """외부(다른 모델·도구)가 참조할 수 있게 JSON Schema 파일로 떨군다."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas", "answers.schema.json")
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(SCHEMA, f, ensure_ascii=False, indent=2)
    return path


if __name__ == "__main__":
    print("규격 파일: %s" % write_schema_file())
