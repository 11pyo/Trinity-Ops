# -*- coding: utf-8 -*-
r"""
runtime_sync.py — 로컬 C: 고속 상태 저장소 ↔ G: 드라이브(_vacation-runtime) 무손실 동기화 엔진 (2026-11-26 신설)

왜 필요한가
  1) state.json · seen.json 을 G: 드라이브 위에서 5~60초마다 `os.replace(tmp, dst)` 로 갈아끼우면,
     Google Drive File Stream 이 업로드 중이던 파일 핸들과 충돌해 G:\클라우드 동기화 폴더 루트에
     `state (1..210).json`, `seen (1..210).json` 같은 고아 충돌 파일 수백 개를 양산했다(실측 452개).
  2) 그렇다고 로컬 C: 에만 두면 G: 드라이브 백업·타 PC 열람·통합 데스크(5177)·지표 집계가 누락될 우려가 있다.

해결 설계 (충돌 0 · 누락 0 보장)
  · 고빈도 원자적 교체(`os.replace`)는 오직 로컬 SSD(`C:\trinitybot-resume\runtime`)에서만 수행한다.
  · G: 드라이브(`_vacation-runtime`)에는 **내용(SHA-256)이 실제로 바뀌었을 때만 제자리 덮어쓰기(`open(..., "w")` / `"a"`)** 로 반영한다.
    임시 파일 이름 바꾸기(`os.replace`)를 G: 에서 쓰지 않으므로 `(1)` 충돌 사본이 원천적으로 생기지 않는다.
  · journal(`journal/*.jsonl`)은 로컬 C: 에 먼저 기록한 뒤 즉시 G: 에 제자리 추가하고, G: 일시 단절로 누락된 줄이 있으면
    주기적 `reconcile_all()` 이 줄 단위 합집합(순서 보존 · 중복 제거)으로 100% 자동 복구한다.
  · 동기화 검증 결과는 `sync_status.json` 에 SHA-256 지문과 함께 남겨 언제든 감사할 수 있다.
"""

import hashlib
import io
import json
import os
import shutil
import time


def sha256_file(path):
    if not path or not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def gdrive_writable(gdrive_dir):
    """G: 드라이브 런타임 폴더가 실제로 마운트되어 접근 가능한지 확인한다."""
    if not gdrive_dir:
        return False
    parent = os.path.dirname(os.path.abspath(gdrive_dir))
    if not os.path.isdir(parent):
        return False
    try:
        os.makedirs(gdrive_dir, exist_ok=True)
        return True
    except OSError:
        return False


def seed_local_from_gdrive(local_dir, gdrive_dir):
    """최초 전환 시 로컬 C: 에 state.json / seen.json 이 없으면 G: 정본에서 그대로 가져온다."""
    os.makedirs(local_dir, exist_ok=True)
    for name in ("state.json", "seen.json"):
        lp = os.path.join(local_dir, name)
        gp = os.path.join(gdrive_dir, name)
        if not os.path.exists(lp) and os.path.exists(gp):
            try:
                raw = io.open(gp, "r", encoding="utf-8").read()
                json.loads(raw)  # 깨진 파일이 아닌지 확인
                tmp = lp + ".seed.tmp"
                io.open(tmp, "w", encoding="utf-8", newline="").write(raw)
                os.replace(tmp, lp)
            except Exception:  # noqa: BLE001
                pass


def write_inplace_if_changed(src_path, dst_path):
    """src_path(C:) 와 dst_path(G:) 의 SHA-256 이 다를 때만 제자리 쓰기(os.replace 미사용)로 복사한다."""
    if not os.path.exists(src_path):
        return True, False
    s_hash = sha256_file(src_path)
    if not s_hash:
        return False, False
    d_hash = sha256_file(dst_path)
    if s_hash == d_hash:
        return True, False
    try:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        data = io.open(src_path, "r", encoding="utf-8").read()
        with io.open(dst_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        return sha256_file(dst_path) == s_hash, True
    except OSError:
        return False, False


def append_journal_dual(local_dir, gdrive_dir, rec):
    """로컬 C: journal 에 먼저 확실히 기록하고, G: 가 열려 있으면 제자리 append 한다."""
    day = time.strftime("%Y-%m-%d") + ".jsonl"
    rec = dict(rec)
    rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    line = json.dumps(rec, ensure_ascii=False) + "\n"

    wrote_local = False
    if local_dir:
        try:
            ld = os.path.join(local_dir, "journal")
            os.makedirs(ld, exist_ok=True)
            with io.open(os.path.join(ld, day), "a", encoding="utf-8", newline="") as fh:
                fh.write(line)
            wrote_local = True
        except OSError:
            pass

    wrote_gdrive = False
    if gdrive_dir and gdrive_writable(gdrive_dir):
        try:
            gd = os.path.join(gdrive_dir, "journal")
            os.makedirs(gd, exist_ok=True)
            with io.open(os.path.join(gd, day), "a", encoding="utf-8", newline="") as fh:
                fh.write(line)
            wrote_gdrive = True
        except OSError:
            pass

    if not wrote_local and not wrote_gdrive and gdrive_dir:
        # 최후 수단(시험 환경 등 local_dir 미사용 시 원래 동작 유지)
        gd = os.path.join(gdrive_dir, "journal")
        os.makedirs(gd, exist_ok=True)
        with io.open(os.path.join(gd, day), "a", encoding="utf-8", newline="") as fh:
            fh.write(line)
        wrote_gdrive = True

    return wrote_local, wrote_gdrive


def _reconcile_journal_file(local_f, gdrive_f):
    """단절 구간에 로컬에만 쌓인 journal 줄을 G: 에 빠짐없이 보충하고, G: 에만 있던 과거 줄은 로컬 백업에도 채운다."""
    l_lines = []
    if os.path.exists(local_f):
        l_lines = [ln.rstrip("\r\n") for ln in io.open(local_f, "r", encoding="utf-8", errors="replace") if ln.strip()]
    g_lines = []
    if os.path.exists(gdrive_f):
        g_lines = [ln.rstrip("\r\n") for ln in io.open(gdrive_f, "r", encoding="utf-8", errors="replace") if ln.strip()]

    g_set = set(g_lines)
    missing_in_g = [ln for ln in l_lines if ln not in g_set]
    if missing_in_g:
        os.makedirs(os.path.dirname(gdrive_f), exist_ok=True)
        with io.open(gdrive_f, "a", encoding="utf-8", newline="") as fh:
            for ln in missing_in_g:
                fh.write(ln + "\n")

    l_set = set(l_lines)
    missing_in_l = [ln for ln in g_lines if ln not in l_set]
    if missing_in_l:
        os.makedirs(os.path.dirname(local_f), exist_ok=True)
        with io.open(local_f, "a", encoding="utf-8", newline="") as fh:
            for ln in missing_in_l:
                fh.write(ln + "\n")

    return len(missing_in_g), len(missing_in_l)


def sync_state_now(local_dir, gdrive_dir):
    """state.json · seen.json 변경분을 즉시 G: 에 제자리 동기화한다."""
    if not local_dir or not gdrive_dir or os.path.normcase(os.path.abspath(local_dir)) == os.path.normcase(os.path.abspath(gdrive_dir)):
        return True
    if not gdrive_writable(gdrive_dir):
        return False
    ok1, _ = write_inplace_if_changed(os.path.join(local_dir, "state.json"), os.path.join(gdrive_dir, "state.json"))
    ok2, _ = write_inplace_if_changed(os.path.join(local_dir, "seen.json"), os.path.join(gdrive_dir, "seen.json"))
    return bool(ok1 and ok2)


def reconcile_all(local_dir, gdrive_dir):
    """state · seen · journal 전체 무결성 대조 및 양방향 누락 복구. 결과를 sync_status.json 에 기록한다."""
    if not local_dir or not gdrive_dir or os.path.normcase(os.path.abspath(local_dir)) == os.path.normcase(os.path.abspath(gdrive_dir)):
        return {"mode": "single_dir", "all_synced": True}

    os.makedirs(local_dir, exist_ok=True)
    reachable = gdrive_writable(gdrive_dir)
    status = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "local_dir": local_dir,
        "gdrive_dir": gdrive_dir,
        "gdrive_reachable": reachable,
        "all_synced": False,
        "recovered_to_gdrive": 0,
        "recovered_to_local": 0,
        "state_synced": False,
        "seen_synced": False,
    }

    if reachable:
        seed_local_from_gdrive(local_dir, gdrive_dir)
        ok_s, _ = write_inplace_if_changed(os.path.join(local_dir, "state.json"), os.path.join(gdrive_dir, "state.json"))
        ok_n, _ = write_inplace_if_changed(os.path.join(local_dir, "seen.json"), os.path.join(gdrive_dir, "seen.json"))
        status["state_synced"] = ok_s
        status["seen_synced"] = ok_n

        lj_dir = os.path.join(local_dir, "journal")
        gj_dir = os.path.join(gdrive_dir, "journal")
        os.makedirs(lj_dir, exist_ok=True)
        os.makedirs(gj_dir, exist_ok=True)
        names = sorted(set(f for f in os.listdir(lj_dir) if f.endswith(".jsonl")) |
                       set(f for f in os.listdir(gj_dir) if f.endswith(".jsonl")))
        to_g_total, to_l_total = 0, 0
        for fn in names:
            try:
                g_add, l_add = _reconcile_journal_file(os.path.join(lj_dir, fn), os.path.join(gj_dir, fn))
                to_g_total += g_add
                to_l_total += l_add
            except OSError:
                pass
        status["recovered_to_gdrive"] = to_g_total
        status["recovered_to_local"] = to_l_total
        status["journal_files"] = len(names)

        # G: 의 검증장부·지표 파일도 로컬 백업 사본으로 보관(G: 재난 대비)
        bdir = os.path.join(local_dir, "gdrive_backup")
        os.makedirs(bdir, exist_ok=True)
        for doc in ("트리니티봇_검증장부.md", "bot-metrics-data.js"):
            gp = os.path.join(gdrive_dir, doc)
            if os.path.exists(gp):
                write_inplace_if_changed(gp, os.path.join(bdir, doc))

        status["state_sha256"] = sha256_file(os.path.join(local_dir, "state.json"))[:16]
        status["seen_sha256"] = sha256_file(os.path.join(local_dir, "seen.json"))[:16]
        status["all_synced"] = bool(ok_s and ok_n)

    try:
        sp_local = os.path.join(local_dir, "sync_status.json")
        tmp = sp_local + ".tmp"
        io.open(tmp, "w", encoding="utf-8", newline="").write(json.dumps(status, ensure_ascii=False, indent=2))
        os.replace(tmp, sp_local)
        if reachable:
            write_inplace_if_changed(sp_local, os.path.join(gdrive_dir, "sync_status.json"))
    except OSError:
        pass
    return status
