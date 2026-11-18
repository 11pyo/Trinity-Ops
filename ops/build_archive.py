#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_archive.py — Compile articles/*/*.md into archive.html and archive-structure.md

Reads markdown SSOT articles, validates schemas and tags, and compiles:
  - archive.html (Read-only mirror)
  - archive-structure.md (Read-only mirror)
"""
import io
import os
import re
import sys
import yaml

# UTF-8 stdout configuration
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTICLES_DIR = os.path.join(ROOT, "articles")
TEMPLATES_DIR = os.path.join(ROOT, "templates")
HTML_OUT = os.path.join(ROOT, "archive.html")
STRUCT_OUT = os.path.join(ROOT, "archive-structure.md")

CATS = ["기초용어", "프로세스", "기술설정", "트러블슈팅", "운영 참조 매뉴얼"]
CAT_DIRS = {
    "기초용어": "01_terms",
    "프로세스": "02_process",
    "기술설정": "03_tech",
    "트러블슈팅": "04_troubleshoot",
    "운영 참조 매뉴얼": "05_manual",
}
CAT_ICONS = {
    "기초용어": "📘 기초용어",
    "프로세스": "🔄 프로세스",
    "기술설정": "⚙️ 기술설정",
    "트러블슈팅": "🔧 트러블슈팅",
    "운영 참조 매뉴얼": "📋 운영 참조 매뉴얼",
}

TAGS_TO_CHECK = ['strong', 'b', 'em', 'a', 'code', 'span', 'small', 'div']

# ── 다크 모드 색 변환 (2026-11-17, Ops Console) ─────────────────────────────
# 아티클 본문 style="…" 과 셸 <style> 의 #hex 색을 빌드 때 var(--t{역할}-{hex}, #원래값) 으로 감싼다.
#   - 밝은 모드: 변수가 정의되지 않아 괄호 안 원래 색이 그대로 쓰인다(겉모습 불변).
#   - 다크 모드: 아래 dark_hex() 가 역할별로 만든 색을 변수로 정의한다(oc-dark-palette).
# 역할: b=바탕(background) · f=글자(color) · l=선(border·outline·shadow). 같은 hex 라도 역할마다 다르게 바뀐다.
# md 정본은 그대로 두고 빌드 때마다 기계적으로 적용 → 새 아티클이 인라인 색을 써도 다크 모드가 자동 대응.
import colorsys

HEX_RE = re.compile(r'#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9A-Za-z_-])')
THEMED_RE = re.compile(r'var\(--t[bfl]-[0-9a-f]+,\s*(#[0-9a-fA-F]{3,8})\)')
DECL_RE = re.compile(r'([-a-zA-Z]+)(\s*:\s*)([^;{}"]*)')

# 자주 쓰는 바탕·글자·선 색은 Ops Console 공통 색 이름으로 연결(templates/ops-console.md)
DARK_TOKEN = {
    ('b', 'ffffff'): 'var(--oc-card)', ('b', 'f0f2f7'): 'var(--oc-bg)', ('b', 'f8fafc'): 'var(--oc-panel)',
    ('f', '2c3e50'): 'var(--oc-ink)', ('f', '1e293b'): 'var(--oc-ink)', ('f', '64748b'): 'var(--oc-sub)',
    ('l', 'e2e8f0'): 'var(--oc-line)', ('l', 'f1f5f9'): 'var(--oc-line)',
}


def _role(prop):
    p = prop.lower()
    if p in ('color', 'caret-color', 'text-decoration-color', 'accent-color'):
        return 'f'
    if p.startswith('background') or p == 'fill':
        return 'b'
    if p.startswith(('border', 'outline', 'box-shadow', 'text-shadow', 'column-rule')) or p == 'stroke':
        return 'l'
    return None


def _hex6(h):
    h = h.lower()
    if len(h) in (3, 4):
        h = ''.join(c * 2 for c in h)
    return h


def dark_hex(role, h):
    """밝은 화면용 색 → 어두운 화면용 색. 바탕은 어둡게(L≤0.25), 글자는 밝게(L≥0.75), 선은 중간."""
    h = _hex6(h)
    rgb, alpha = h[:6], h[6:]
    r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hh, l, s = colorsys.rgb_to_hls(r, g, b)
    if role == 'b':
        l2 = 0.11 + (1 - l) * 0.34 if l > 0.5 else min(l, 0.25)
        s2 = s * 0.7
    elif role == 'f':
        l2 = 0.93 - l * 0.3 if l < 0.6 else max(l, 0.75)
        s2 = min(s, 0.8)
    else:
        l2 = 0.2 + (1 - l) * 0.4 if l > 0.75 else (l if l > 0.5 else max(l, 0.42))   # 옅은 선만 어둡게, 강조선은 유지
        s2 = s * 0.8
    r2, g2, b2 = colorsys.hls_to_rgb(hh, l2, s2)
    return '#' + ''.join(f'{round(c * 255):02x}' for c in (r2, g2, b2)) + alpha


def _lum(rgb):
    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


LIGHT_REF_BG = (0xed / 255, 0xf1 / 255, 0xf6 / 255)   # 아카이브에서 가장 흔한 연한 상자 바탕보다 조금 더 어두운 기준


def light_hex(h):
    """밝은 화면에서 연한 바탕 기준 4.5:1 미만인 글자색 → 같은 색조로 4.6:1 이 될 때까지 어둡게. 통과하면 None."""
    h = _hex6(h)
    if len(h) != 6:
        return None
    rgb = tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    bg = _lum(LIGHT_REF_BG)
    if (bg + 0.05) / (_lum(rgb) + 0.05) >= 4.5:
        return None
    hh, l, s = colorsys.rgb_to_hls(*rgb)
    if l > 0.86:                     # 흰색·아주 밝은 글자는 진한 바탕(배지 · 수식 상자 · 표 머리) 위 글자 — 건드리지 않는다
        return None
    while l > 0:
        l = max(0.0, l - 0.01)
        c = colorsys.hls_to_rgb(hh, l, s)
        if (bg + 0.05) / (_lum(c) + 0.05) >= 4.6:
            return '#' + ''.join(f'{round(x * 255):02x}' for x in c)
    return '#000000'


def _theme_decls(text, used):
    def decl(m):
        role = _role(m.group(1))
        if not role:
            return m.group(0)

        def wrap(hm):
            key = _hex6(hm.group(1))
            used.add((role, key))
            return f'var(--t{role}-{key}, {hm.group(0)})'
        return m.group(1) + m.group(2) + HEX_RE.sub(wrap, m.group(3))
    return DECL_RE.sub(decl, THEMED_RE.sub(r'\1', text))


def apply_dark_theme(html):
    used = set()
    # 1) 셸 <style> 블록(공통 색 이름 블록 oc-console 제외): 중괄호 안 선언만
    def style_block(m):
        if 'oc-console' in m.group(1):
            return m.group(0)
        body = re.sub(r'\{([^{}]*)\}', lambda b: '{' + _theme_decls(b.group(1), used) + '}', m.group(2))
        return m.group(1) + body + m.group(3)
    html = re.sub(r'(<style[^>]*>)([\s\S]*?)(</style>)', style_block, html)
    # 2) 인라인 style="…" 속성(아티클 본문 · 셸 JS 문자열 포함)
    html = re.sub(r'(\sstyle\s*=\s*")([^"]*)(")', lambda m: m.group(1) + _theme_decls(m.group(2), used) + m.group(3), html)
    html = re.sub(r"(\sstyle\s*=\s*')([^']*)(')", lambda m: m.group(1) + _theme_decls(m.group(2), used) + m.group(3), html)
    vars_css = ';'.join(f'--t{r}-{h}:{DARK_TOKEN.get((r, h)) or dark_hex(r, h)}' for r, h in sorted(used))
    # 밝은 모드 대비 보정(2026-11-18): 연한 바탕(#edf1f6 기준) 위에서 4.5:1 이 안 되는 글자색만 같은 색조로 진하게.
    # 같은 hex 가 진한 바탕 위에서도 쓰이면(셸 머리말 · 수식 상자 등) 셸 템플릿의 sd-light-keep 블록이 원래 색을 지킨다.
    light_css = ';'.join(f'--t{r}-{h}:{light_hex(h)}' for r, h in sorted(used) if r == 'f' and light_hex(h))
    palette = ('<style id="oc-dark-palette">/* build_archive.py 생성 — 다크 모드 색표(직접 편집 금지) */\n'
               '@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){' + vars_css + '}}\n'
               '@media not (prefers-color-scheme:dark){:root{' + light_css + '}}\n'
               ':root[data-theme="light"]{' + light_css + '}</style>\n')
    html = re.sub(r'<style id="oc-dark-palette">/\* build_archive\.py [^<]*</style>\n?', '', html)   # 재빌드 시 옛 색표 제거(생성본 형태만)
    return html.replace('</head>', palette + '</head>', 1), len(used)


def parse_markdown_article(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read().lstrip("\ufeff").replace("\r\n", "\n")

    # Split frontmatter and body
    m = re.match(r'^---\s*\n([\s\S]*?)\n---\s*\n([\s\S]*)$', text)
    if not m:
        raise ValueError(f"Invalid frontmatter format in {file_path}")

    fm_raw = m.group(1)
    body = m.group(2)

    try:
        fm = yaml.safe_load(fm_raw) or {}
    except Exception as e:
        raise ValueError(f"YAML parse error in {file_path}: {e}")

    return fm, body


def validate_article(fm, body, file_path, seen_ids):
    aid = fm.get("id")
    if not aid:
        print(f"❌ Error in {file_path}: Missing 'id' in frontmatter")
        return False

    if aid in seen_ids:
        print(f"❌ Error: Duplicate article id '{aid}' in {file_path} (already in {seen_ids[aid]})")
        return False
    seen_ids[aid] = file_path

    # Check for nested <article>
    if "<article" in body.lower():
        print(f"❌ Error in {file_path}: Nested <article> found in body")
        return False

    # Check balanced tags
    for tag in TAGS_TO_CHECK:
        open_count = len(re.findall(rf'<{tag}(\s+[^>]*)?>', body, re.IGNORECASE))
        close_count = len(re.findall(rf'</{tag}\s*>', body, re.IGNORECASE))
        if open_count != close_count:
            print(f"❌ Error in {file_path}: Unbalanced <{tag}> tag (open={open_count}, close={close_count})")
            return False

    return True


def main():
    print("=" * 60)
    print("Building archive mirrors from markdown SSOT...")
    print("=" * 60)

    # 1. Load templates
    shell_path = os.path.join(TEMPLATES_DIR, "archive_shell.html")
    update_path = os.path.join(TEMPLATES_DIR, "latest_update.txt")
    footer_path = os.path.join(TEMPLATES_DIR, "structure_footer.md")

    if not os.path.exists(shell_path) or not os.path.exists(update_path) or not os.path.exists(footer_path):
        print(f"❌ Error: Templates missing in {TEMPLATES_DIR}. Run decompile_archive.py first.")
        sys.exit(1)

    with open(shell_path, "r", encoding="utf-8") as f:
        shell_html = f.read()
    with open(update_path, "r", encoding="utf-8") as f:
        latest_update = f.read().strip()
    with open(footer_path, "r", encoding="utf-8") as f:
        structure_footer = f.read()

    # 2. Collect and validate articles
    seen_ids = {}
    articles_by_cat = {c: [] for c in CATS}

    if not os.path.exists(ARTICLES_DIR):
        print(f"❌ Error: {ARTICLES_DIR} does not exist.")
        sys.exit(1)

    for cat_name in CATS:
        cat_dir_name = CAT_DIRS[cat_name]
        cat_path = os.path.join(ARTICLES_DIR, cat_dir_name)
        if not os.path.exists(cat_path):
            continue

        for fname in os.listdir(cat_path):
            if fname.endswith(".md"):
                fpath = os.path.join(cat_path, fname)
                try:
                    fm, body = parse_markdown_article(fpath)
                except Exception as e:
                    print(f"❌ Error: {e}")
                    sys.exit(1)

                if not validate_article(fm, body, fpath, seen_ids):
                    sys.exit(1)

                # Ensure category is set
                fm_cat = fm.get("category", cat_name)
                articles_by_cat[fm_cat].append((fm, body, fpath))

    # 3. Sort articles within each category
    total_articles = sum(len(articles_by_cat[c]) for c in CATS)
    print(f"Loaded and validated {total_articles} articles across {len(CATS)} categories.")

    for c in CATS:
        # Sort by (order, id)
        articles_by_cat[c].sort(key=lambda item: (item[0].get("order", 9999), item[0].get("id", "")))

    # 4. Generate Sidebar Navigation HTML
    sidebar_lines = []
    sidebar_links_count = 0
    for c in CATS:
        sidebar_lines.append(f'  <div class="nav-cat-title">{CAT_ICONS[c]}</div>')
        for fm, _, _ in articles_by_cat[c]:
            # Rule: Render in sidebar only if collapsed is false
            if not fm.get("collapsed", False):
                aid = fm["id"]
                nav_title = fm.get("nav_title") or fm.get("title", "")
                nav_desc = fm.get("nav_desc", "")
                if nav_desc:
                    sidebar_lines.append(f'  <a class="nav-link" href="#{aid}">{nav_title}<span class="nd">{nav_desc}</span></a>')
                else:
                    sidebar_lines.append(f'  <a class="nav-link" href="#{aid}">{nav_title}</a>')
                sidebar_links_count += 1

    sidebar_nav_html = "\n" + "\n".join(sidebar_lines) + "\n"

    # 5. Generate Articles Content HTML (with Auto-Lineage Knowledge Graph compilation)
    id_regex = re.compile(
        r'(?<![A-Za-z0-9_-])('
        + '|'.join(re.escape(k) for k in sorted(seen_ids.keys(), key=len, reverse=True))
        + r')(?![A-Za-z0-9_-])'
    ) if seen_ids else None
    graph_fwd_nodes = set()
    graph_bk_nodes = set()
    graph_edge_count = 0

    content_blocks = []
    for c in CATS:
        cnt = len(articles_by_cat[c])
        content_blocks.append(f'<!-- ═══════════════════════════════════ {c} ═══ -->')
        content_blocks.append(f'<div class="cat-header"><h2>{CAT_ICONS[c]}</h2><span class="cnt">{cnt}</span></div>')
        content_blocks.append('')

        for fm, body, _ in articles_by_cat[c]:
            aid = fm["id"]
            title = fm.get("title", "")
            badge_class = fm.get("badge_class", "b-기초")
            badge_text = fm.get("badge_text", c)
            date = fm.get("date", "")
            collapsed = fm.get("collapsed", False)
            old = fm.get("old", False)

            # Class string
            cls_parts = ["article"]
            if collapsed:
                cls_parts.append("collapsed")
            if old:
                cls_parts.append("old")
            cls_str = " ".join(cls_parts)

            # Auto-Lineage Knowledge Graph: merge explicit data_related + valid article IDs referenced in struct_keywords/body
            rel_list = []
            for r_id in str(fm.get("data_related") or "").split():
                if r_id and r_id != aid and r_id in seen_ids and r_id not in rel_list:
                    rel_list.append(r_id)
            if id_regex:
                scan_source = f"{fm.get('struct_keywords') or ''}\n{body}"
                for r_id in id_regex.findall(scan_source):
                    if r_id != aid and r_id in seen_ids and r_id not in rel_list:
                        rel_list.append(r_id)
                        if len(rel_list) >= 12:
                            break

            # Data attributes
            data_attrs = []
            if fm.get("data_cat"):
                data_attrs.append(f'data-cat="{fm["data_cat"]}"')
            if rel_list:
                data_attrs.append(f'data-related="{" ".join(rel_list)}"')
                graph_fwd_nodes.add(aid)
                graph_bk_nodes.update(rel_list)
                graph_edge_count += len(rel_list)
            if fm.get("data_tags"):
                data_attrs.append(f'data-tags="{fm["data_tags"]}"')

            data_attrs_str = (" " + " ".join(data_attrs)) if data_attrs else ""

            # Tag chips
            tag_chips = fm.get("tag_chips") or []
            chips_html = "".join(f'<span class="tag">{chip}</span>' for chip in tag_chips)

            # Assemble article
            art_lines = [
                f'<article class="{cls_str}" id="{aid}"{data_attrs_str}>',
                '  <div class="ah">',
                f'    <div class="at">{title}</div>',
                f'    <span class="badge {badge_class}">{badge_text}</span>',
                f'    <span class="db">{date}</span>',
                '  </div>',
                '  <div class="tags">',
                f'    {chips_html}',
                '  </div>',
                body.strip(),
                '</article>'
            ]
            content_blocks.append("\n".join(art_lines))
            content_blocks.append('')

    articles_content_html = "\n".join(content_blocks)

    # 6. Build archive.html
    final_html = shell_html.replace('<!-- {{SIDEBAR_NAV}} -->', sidebar_nav_html.strip('\n'))
    final_html = final_html.replace('<!-- {{ARTICLES_CONTENT}} -->', articles_content_html)
    final_html, themed_colors = apply_dark_theme(final_html)
    print(f"Dark mode: {themed_colors} role-colors themed (oc-dark-palette)")

    with open(HTML_OUT, "w", encoding="utf-8") as f:
        f.write(final_html)
    connected_nodes = len(graph_fwd_nodes | graph_bk_nodes)
    print(f"Generated {HTML_OUT} ({len(final_html)} bytes, {sidebar_links_count} sidebar links, Knowledge Graph: {connected_nodes}/{total_articles} connected [{len(graph_fwd_nodes)} fwd, {graph_edge_count} edges])")

    # 7. Build archive-structure.md
    struct_lines = [
        "# archive.html 구조 인덱스",
        "> **목적**: 매 세션마다 archive.html 전체를 읽지 않도록 구조를 미리 파악하기 위한 경량 인덱스.  ",
        "> **업데이트 규칙**: archive.html에 아티클 추가·삭제·이동 시 이 파일도 반드시 동기화할 것.  ",
        f"{latest_update}",
        "",
        "---",
        "",
        "## 파일 위치",
        "```",
        "archive.html (아카이브 루트 폴더)",
        "```",
        "",
        "---",
        "",
        "## 전체 아티클 수",
        "| 카테고리 | 아티클 수 | HTML 변수 (cnt) |",
        "|----------|-----------|----------------|",
    ]

    for c in CATS:
        cnt = len(articles_by_cat[c])
        struct_lines.append(f'| {CAT_ICONS[c]} | {cnt} | `<span class="cnt">{cnt}</span>` |')
    struct_lines.append(f'| **합계** | **{total_articles}** | |')
    struct_lines.append('')
    struct_lines.append('> **🔗 메타·링크 레이어 (2026-06-24, Obsidian 패턴 융합)**: 일부 핵심 아티클에 `data-related`(관련글 id 공백구분)·`data-cat` 속성이 추가됨(본문 무손상·검색 영향 0·아티클 수 불변). 페이지 하단 인라인 JS가 이를 읽어 글 하단에 「🔗 관련 / ↩ 여기를 참조」 backlink 칩 + `#태그` 클릭검색을 자동 렌더(`data-noexport`=💾저장 제외, 정본=`data-related` 속성뿐). 통제 태그어휘·메타 규칙=`META_TAG_SCHEMA.md`. 현재 부여 15아티클(여신/세금/반품/취소 허브). 알고리즘 상세=블루프린트 `03-algorithms-scaling`.')
    struct_lines.append('')
    struct_lines.append('---')
    struct_lines.append('')

    # Category tables
    for c in CATS:
        cnt = len(articles_by_cat[c])
        struct_lines.append(f'## {CAT_ICONS[c]} ({cnt}개)')
        struct_lines.append('')
        struct_lines.append('| # | id | 제목 | 핵심 키워드 |')
        struct_lines.append('|---|----|----|------------|')

        for idx, (fm, _, _) in enumerate(articles_by_cat[c], start=1):
            aid = fm["id"]
            stitle = fm.get("struct_title") or fm.get("title", "")
            keywords = fm.get("struct_keywords")
            if not keywords:
                # Combine tag chips and data_tags
                tag_chips = fm.get("tag_chips") or []
                keywords = ", ".join(tag_chips) if tag_chips else fm.get("data_tags", "")
            # Escape pipes if any
            stitle = stitle.replace("|", "&#124;")
            keywords = keywords.replace("|", "&#124;")
            struct_lines.append(f'| {idx} | `{aid}` | {stitle} | {keywords} |')

        struct_lines.append('')
        struct_lines.append('---')
        struct_lines.append('')

    # Append footer
    struct_lines.append(structure_footer.strip())
    struct_lines.append('')

    final_struct = "\n".join(struct_lines)
    with open(STRUCT_OUT, "w", encoding="utf-8") as f:
        f.write(final_struct)
    print(f"Generated {STRUCT_OUT} ({len(final_struct)} bytes, {total_articles} rows)")

    print("\n✅ Build complete: archive.html and archive-structure.md successfully compiled.")


if __name__ == "__main__":
    main()
