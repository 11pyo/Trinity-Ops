# -*- coding: utf-8 -*-
"""복귀 보고서 HTML 변환 (2026-10-08 · 담당자 지시: 「복귀 보고서는 html 로 나와 AI 가 동시에 보기 편하게」).

한 파일에 두 독자가 같이 쓴다:
  · 사람 — 읽기 쉬운 화면(요약 타일 · 목차 · 문의별 카드 · 답변 복사 버튼 · 어두운 화면 지원).
  · AI  — `<script type="application/json" id="report-data">` 에 같은 내용을 **구조화**해 둔다(요약 · 문의별 필드 · 표 · 목록).
          카드에는 data-kind · data-id · data-section 속성도 붙어 있어 HTML 만 읽어도 위치를 잡을 수 있다.

mode.py 의 보고서는 규칙적으로 만들어진 Markdown 이다. 그 글을 **그대로 변환**한다 — 보고서 작성 코드를 건드리지 않아
내용이 둘로 갈라지지 않는다(Markdown 은 옆에 그대로 남는다). 요청자가 쓴 글이 섞여 있으므로 **모든 글자를 이스케이프**한다.
"""
import html
import io
import json
import re
import time

# ───────────────────────────────────────────── 구조화(AI 용)
_ITEM_RX = re.compile(r"^### `(INQ-[\w-]+)`(?: — (.*))?$")
_KV_RX = re.compile(r"^\*\*(.+?)\*\*\s*[:：]\s*(.*)$")
_BOLD_ONLY_RX = re.compile(r"^\*\*(.+?)\*\*\s*$")
_FIELD_KEYS = {
    "앞선 답": "previous_answer",
    "문의": "question",
    "담당자용 메모(현업 미노출)": "owner_note",
    "확인하실 것": "to_do",
    "현재 기록": "current_state",
}


def _cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_sep(line):
    return bool(re.match(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", line.strip()))


def parse_report(md):
    """보고서 Markdown → 구조화 dict. 모르는 구역도 버리지 않고 `sections[].blocks` 에 텍스트로 남긴다."""
    lines = md.splitlines()
    data = {"title": "", "summary": {}, "sections": [], "schema": "trinitybot-return-report/1"}
    sec = None
    item = None
    label = ""
    i = 0
    n = len(lines)

    def new_section(title):
        s = {"title": title, "items": [], "tables": [], "bullets": [], "text": []}
        data["sections"].append(s)
        return s

    while i < n:
        ln = lines[i]
        if ln.startswith("# ") and not data["title"]:
            data["title"] = ln[2:].strip()
        elif ln.startswith("## "):
            sec, item = new_section(ln[3:].strip()), None
        elif _ITEM_RX.match(ln) and sec is not None:
            m = _ITEM_RX.match(ln)
            item = {"id": m.group(1), "requester": (m.group(2) or "").strip()}
            sec["items"].append(item)
        elif ln.startswith("|") and i + 1 < n and _is_sep(lines[i + 1]):
            head = _cells(ln)
            rows = []
            i += 2
            while i < n and lines[i].startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            if sec is None:                                   # 맨 위 요약표 (항목 | 값)
                for r in rows:
                    if len(r) >= 2:
                        data["summary"][re.sub(r"[*`]", "", r[0])] = re.sub(r"\*\*", "", r[1])
            else:
                sec["tables"].append({"columns": head, "rows": [dict(zip(head, r)) for r in rows]})
            continue
        elif ln.strip().startswith("```"):
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            if item is not None:
                txt = "\n".join(body).strip()
                if label.startswith("담당자가 올린 답"):
                    item.setdefault("owner_answers", []).append({"label": label, "text": txt})
                else:
                    item["public_answer"] = txt
        elif ln.startswith("- ") and sec is not None and item is None:
            sec["bullets"].append(ln[2:].strip())
        elif ln.startswith("  ") and ln.strip() and sec is not None and item is None and sec["bullets"] and not sec["text"]:
            sec["bullets"][-1] += " " + ln.strip()                       # 들여쓴 이어지는 줄은 앞 항목에 붙인다
        elif item is not None and _BOLD_ONLY_RX.match(ln):
            label = _BOLD_ONLY_RX.match(ln).group(1).strip()              # 뒤따르는 코드 블록이 무엇인지(현업 답변 / 담당자가 올린 답)
        elif item is not None and _KV_RX.match(ln):
            k, v = _KV_RX.match(ln).groups()
            item[_FIELD_KEYS.get(k.strip(), k.strip())] = v.strip()
        elif sec is not None and item is None and ln.strip() and ln.strip() != "---":
            sec["text"].append(ln.strip())
        i += 1
    for s in data["sections"]:
        for k in ("items", "tables", "bullets", "text"):
            if not s[k]:
                del s[k]
    return data


# ───────────────────────────────────────────── 사람 용 HTML
def _inline(text):
    """이스케이프 후에만 서식을 입힌다 — 요청자가 쓴 < > & 가 태그로 읽히지 않게."""
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return t


def _slug(s, used):
    base = re.sub(r"[^0-9A-Za-z가-힣]+", "-", s).strip("-")[:40] or "sec"
    out, k = base, 2
    while out in used:
        out, k = "%s-%d" % (base, k), k + 1
    used.add(out)
    return out


_CSS = """
:root{--bg:#f6f7f9;--fg:#1c2330;--mut:#5b6678;--card:#fff;--bd:#dfe3ea;--ac:#2b5fd9;--ok:#1d7a46;--warn:#b25b00;--bad:#b3261e;--code:#eef1f6}
@media (prefers-color-scheme:dark){:root{--bg:#12161d;--fg:#e6e9ef;--mut:#9aa5b8;--card:#1a2029;--bd:#2c3441;--ac:#7aa2ff;--ok:#5fd08c;--warn:#f2a85a;--bad:#ff8a80;--code:#222a36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:.2em 0 .6em}h2{font-size:18px;margin:1.6em 0 .6em;padding-bottom:.3em;border-bottom:1px solid var(--bd)}
h3{font-size:15px;margin:0}p{margin:.5em 0}code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.92em}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:14px 0}.tile{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px 12px}
.tile b{display:block;font-size:20px}.tile span{color:var(--mut);font-size:12px}.tile.warn b{color:var(--warn)}.tile.bad b{color:var(--bad)}.tile.ok b{color:var(--ok)}
nav.toc{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px 14px;margin:12px 0}nav.toc a{color:var(--ac);text-decoration:none;margin-right:12px;white-space:nowrap}
table{border-collapse:collapse;width:100%;background:var(--card);margin:10px 0;font-size:13.5px;display:block;overflow-x:auto}th,td{border:1px solid var(--bd);padding:6px 9px;text-align:left;vertical-align:top}th{background:var(--code)}
article.item{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px;margin:14px 0}article.item header{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.id{font-family:Consolas,monospace;font-size:13px;background:var(--code);padding:2px 6px;border-radius:5px}.who{color:var(--mut)}
.lab{display:inline-block;font-size:12px;color:var(--mut);margin-top:8px}pre.answer{background:var(--code);border-radius:8px;padding:10px 12px;white-space:pre-wrap;word-break:break-word;margin:6px 0;position:relative}
button.cp{position:absolute;top:6px;right:6px;font-size:12px;border:1px solid var(--bd);background:var(--card);color:var(--fg);border-radius:6px;padding:2px 8px;cursor:pointer}
details{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:8px 12px;margin:10px 0}summary{cursor:pointer;font-weight:600}
.todo{border-left:3px solid var(--warn);padding-left:10px;margin-top:8px}hr{border:0;border-top:1px solid var(--bd);margin:18px 0}ul{padding-left:1.2em}
.meta{color:var(--mut);font-size:12.5px}@media print{button.cp{display:none}body{background:#fff;color:#000}}
"""

_JS = """
document.querySelectorAll('button.cp').forEach(function(b){b.addEventListener('click',function(){
  var t=b.parentElement.querySelector('code').innerText;
  (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){b.textContent='복사됨'},function(){b.textContent='복사 실패'});
  setTimeout(function(){b.textContent='복사'},1500);});});
"""

_TILE_KEYS = [("처리 대상 문의", ""), ("AI 가 답변 기재", ""), ("대기(판단 보류)", "warn"), ("아직 열려 있는 건", "warn"),
              ("호출 실패", "bad"), ("접수 창구 끊김", "bad"), ("오류·실패", "bad"), ("Claude 호출", "")]


def _tile_class(key, val, base):
    v = re.sub(r"[*]", "", val)
    nums = re.findall(r"\d+", v)
    zero = (not nums) or all(int(x) == 0 for x in nums[:1]) or v.strip() in ("없음", "0건")
    if base and not zero:
        return base
    return "ok" if base and zero else ""


def render_html(md, title=None, generated=None):
    data = parse_report(md)
    title = title or data.get("title") or "부재중 처리 결과"
    generated = generated or time.strftime("%Y-%m-%d %H:%M:%S")
    used = set()
    out = []
    toc = []
    lines = md.splitlines()
    i, n = 0, len(lines)
    open_item = False
    h1 = "<h1>%s</h1>" % html.escape(title)

    def close_item():
        nonlocal open_item
        if open_item:
            out.append("</article>")
            open_item = False

    section = ""
    while i < n:
        ln = lines[i]
        if ln.startswith("# "):
            pass                                             # 제목은 맨 위(요약 타일 앞)에 따로 둔다
        elif ln.startswith("## "):
            close_item()
            section = ln[3:].strip()
            sid = _slug(section, used)
            toc.append((sid, section))
            out.append('<h2 id="%s" data-kind="section">%s</h2>' % (sid, _inline(section)))
        elif _ITEM_RX.match(ln):
            close_item()
            m = _ITEM_RX.match(ln)
            sid = _slug(m.group(1), used)
            toc.append((sid, m.group(1)))
            out.append('<article class="item" id="%s" data-kind="item" data-id="%s" data-section="%s"><header><h3><span class="id">%s</span></h3><span class="who">%s</span></header>'
                       % (sid, html.escape(m.group(1)), html.escape(section, quote=True), html.escape(m.group(1)), _inline(m.group(2) or "")))
            open_item = True
        elif ln.startswith("|") and i + 1 < n and _is_sep(lines[i + 1]):
            head = _cells(ln)
            rows = []
            i += 2
            while i < n and lines[i].startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            t = "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % _inline(h) for h in head),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r) for r in rows))
            if "행동 기록" in section or "오류·실패" in section:
                out.append("<details><summary>%s (펼치기)</summary>%s</details>" % (_inline(section), t))
            else:
                out.append(t)
            continue
        elif ln.strip().startswith("```"):
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            out.append('<pre class="answer"><button class="cp" type="button">복사</button><code>%s</code></pre>' % html.escape("\n".join(body)))
        elif ln.startswith("- "):
            items = [ln[2:].strip()]
            while i + 1 < n and (lines[i + 1].startswith("- ") or lines[i + 1].startswith("  ")) and lines[i + 1].strip():
                i += 1
                if lines[i].startswith("- "):
                    items.append(lines[i][2:].strip())
                else:
                    items[-1] += " " + lines[i].strip()
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _inline(x) for x in items))
        elif ln.strip() == "---":
            close_item()
            out.append("<hr>")
        elif ln.strip():
            if _BOLD_ONLY_RX.match(ln):
                out.append('<span class="lab">%s</span>' % _inline(_BOLD_ONLY_RX.match(ln).group(1)))
            elif ln.startswith("**확인하실 것**"):
                out.append('<p class="todo">%s</p>' % _inline(ln))
            elif ln.startswith("**담당자용 메모"):
                out.append("<details><summary>담당자용 메모 (현업 미노출)</summary><p>%s</p></details>" % _inline(re.sub(r"^\*\*담당자용 메모[^*]*\*\*\s*[:：]\s*", "", ln)))
            else:
                out.append("<p>%s</p>" % _inline(ln))
        i += 1
    close_item()

    tiles = []
    for key, base in _TILE_KEYS:
        for k, v in data["summary"].items():
            if k.startswith(key):
                tiles.append('<div class="tile %s"><b>%s</b><span>%s</span></div>' % (_tile_class(k, v, base), html.escape(re.sub(r"\s+", " ", v)[:40]), html.escape(k)))
                break
    nav = '<nav class="toc"><strong>목차</strong> ' + "".join('<a href="#%s">%s</a>' % (s, html.escape(t)) for s, t in toc[:60]) + "</nav>" if toc else ""
    # JSON 안의 < > & 는 유니코드 이스케이프로 바꾼다 — 요청자가 쓴 <script> 글자가 문서 구조에 영향을 주지 못하게(JSON 값은 그대로 복원된다)
    payload = (json.dumps(dict(data, generated=generated), ensure_ascii=False)
               .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    return ("<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title><style>%s</style></head><body><main>\n"
            "<!-- AI 용: 이 문서의 구조화 본문은 문서 끝의 JSON 스크립트 블록(id=report-data)에 있다. 문의는 article[data-kind=item][data-id], "
            "구역은 h2[data-kind=section] 로 찾는다. 같은 내용의 Markdown 이 이 파일 옆에 있다. -->\n"
            "%s\n<p class=\"meta\">생성 %s · 담당자용 메모는 현업에게 나가지 않습니다 · 이 파일은 읽기용입니다(수정·판정은 대시보드에서)</p>\n"
            "<div class=\"tiles\">%s</div>\n%s\n%s\n</main>\n"
            "<script id=\"report-data\" type=\"application/json\">%s</script>\n<script>%s</script></body></html>\n"
            % (html.escape(title), _CSS, h1, html.escape(generated), "".join(tiles), nav, "\n".join(out), payload, _JS))


def write_html(md, md_path):
    """보고서 Markdown 옆에 같은 이름의 .html 을 쓴다. 돌려주는 값 = HTML 경로. 실패하면 예외를 그대로 낸다(부른 쪽이 Markdown 으로 되돌린다)."""
    hp = re.sub(r"\.md$", ".html", md_path)
    if hp == md_path:
        hp = md_path + ".html"
    io.open(hp, "w", encoding="utf-8", newline="").write(render_html(md))
    return hp
