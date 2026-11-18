# -*- coding: utf-8 -*-
"""
accounts.py — 현업 프로파일 저장소 (사번 → 성함 · 소속 · 담당 업무)

    python accounts.py list  [--test]          → 등록된 사번 목록
    python accounts.py show  <사번> [--test]    → 한 사람의 프로파일
    python accounts.py clear-pin <사번> [--test] → 잊은 선택 PIN 을 해제한다(프로파일은 유지)
    python accounts.py reset <사번> [--test]    → 그 사번의 등록을 지운다(잘못 등록 · 남의 사번 선점 정리용 — 백업 후)

저장 위치 — **공유 아카이브 밖**(첨부 사진 폴더와 같은 줄):
    운영  …\\회사 업무\\SAP SD\\_intake-profiles\\profiles.json
    시험  …\\회사 업무\\SAP SD\\_intake-profiles-chat-test\\profiles.json
사번 · 성함 · 소속은 개인 정보다. 아카이브(SAP SD AI Indexable Archive) 안에 두지 않는다.

⛔ 지키는 것
  · 세션 토큰 · 평문 비밀번호는 저장하지 않는다(로그인 세션은 서버 메모리에만 있고, 선택 PIN 은 무작위 솔트+PBKDF2 해시만 저장한다).
  · 프로세스 간 잠금(profiles.json.lock) + 쓰기 직전 `.prev` + 일자별 백업, 임시 파일 → 교체(원자적), 쓴 뒤 다시 읽어 확인.
  · 파일이 깨져 있으면 덮어쓰지 않는다 — 깨진 파일을 따로 떼어 두고 `.prev` 로 이어 간다.
  · 문의 기록(inquiry-log.js)은 여기서 고치지 않는다. 대화의 정본은 문의 로그이고, 이 파일은 「누가 누구인가」만 안다.
"""
import contextlib
import copy
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None

EMP_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,19}$")
PIN_RE = re.compile(r"^\d{4,8}$")
PIN_ITER = 120000
MAX_NAME, MAX_ORG, MAX_ROLE = 40, 60, 40
MAX_IDS = 2000                      # 한 사람에게 이어 둘 문의 번호 상한(무한 증가 방지)
WORK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def folder(test=False):
    return os.path.join(WORK, ".runtime", "_intake-profiles-chat-test" if test else "_intake-profiles")


def norm_emp(s):
    """사번 — 공백을 지우고 대문자로. 영문 · 숫자 · 하이픈 3~20자가 아니면 빈 값."""
    if not isinstance(s, str):
        return ""
    s = re.sub(r"\s+", "", s).upper()
    return s if EMP_RE.match(s) else ""


def norm_pin(s):
    """선택 PIN — 숫자 4~8자리가 아니면 빈 값."""
    if not isinstance(s, str):
        return ""
    s = re.sub(r"\s+", "", s)
    return s if PIN_RE.match(s) else ""


def _hash_pin(pin, salt_hex=None):
    # [SECURE] PBKDF2-HMAC-SHA256 with random 16-byte salt for optional user PIN (Category 2)
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PIN_ITER)
    return salt.hex(), dk.hex()


def has_pin(p):
    return bool(isinstance(p, dict) and p.get("pin_hash") and p.get("pin_salt"))


def verify_pin(p, pin):
    pin = norm_pin(pin)
    if not pin or not has_pin(p):
        return False
    try:
        _, cand = _hash_pin(pin, p["pin_salt"])
    except ValueError:
        return False
    # [SECURE] Constant-time hash comparison prevents timing side-channel (Category 2)
    return hmac.compare_digest(cand, p["pin_hash"])


def clean(s, limit):
    # [SECURE] Type check, control/bidi character strip, middle-dot sanitization and length cap (Category 1)
    if not isinstance(s, str):
        return ""
    s = re.sub(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]", "", s)
    s = s.replace("·", " ")
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def aliases(p):
    """이 사람이 쓴 적 있는 (성함, 소속) — 지금 것 먼저. 소속을 바꿔도 예전 문의가 이어서 보이게."""
    out, seen = [], set()
    for n, o in [(p.get("name", ""), p.get("org", ""))] + [tuple(x) for x in (p.get("aliases") or []) if isinstance(x, list) and len(x) == 2]:
        k = re.sub(r"\s+", "", "%s·%s" % (n, o)).lower()
        if n and k not in seen:
            seen.add(k)
            out.append((n, o))
    return out


def public(p):
    return {"emp": p.get("emp", ""), "name": p.get("name", ""), "org": p.get("org", ""),
            "role": p.get("role", ""), "pin_set": has_pin(p)}


class Exists(Exception):
    pass


class Store(object):
    def __init__(self, path_dir):
        self.dir = path_dir
        self.path = os.path.join(path_dir, "profiles.json")
        self.lock_path = os.path.join(path_dir, "profiles.json.lock")
        self._lock = threading.Lock()
        self._fl_depth = 0
        self._cache, self._sig = None, None

    @contextlib.contextmanager
    def _file_lock(self, create=False):
        # [SECURE] Reentrant (within self._lock) cross-process OS file lock on profiles.json.lock (Category 3)
        if not create and not os.path.exists(self.dir):
            yield
            return
        if self._fl_depth > 0:
            self._fl_depth += 1
            try:
                yield
            finally:
                self._fl_depth -= 1
            return
        os.makedirs(self.dir, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                for _ in range(50):
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(0.05)
                if not locked:
                    raise TimeoutError("프로파일 잠금 대기 시간이 초과되었습니다")
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            self._fl_depth += 1
            yield
        finally:
            if self._fl_depth > 0 and locked:
                self._fl_depth -= 1
            if locked:
                try:
                    if msvcrt is not None:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    elif fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)

    # ── 읽기 ──────────────────────────────────────────────
    def _parse(self, path):
        with io.open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        profs = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profs, dict):
            raise ValueError("프로파일 파일 형식이 맞지 않습니다")
        return profs

    def _load(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._cache, self._sig = {}, None
            return {}
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig and self._cache is not None:
            return self._cache
        try:
            profs = self._parse(self.path)
        except (ValueError, OSError) as e:
            with self._file_lock(create=True):
                try:
                    profs = self._parse(self.path)
                except (ValueError, OSError):
                    # 깨진 파일은 덮어쓰지 않는다 — 떼어 두고 직전본으로 이어 간다
                    bad = self.path + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S")
                    sys.stdout.write("  [!] 프로파일 파일을 읽지 못했습니다(%s) — %s 로 옮기고 직전본을 씁니다\n" % (e, os.path.basename(bad)))
                    shutil.copyfile(self.path, bad)
                    profs = self._parse(self.path + ".prev") if os.path.exists(self.path + ".prev") else {}
                    self._write(profs)
                    return self._load()
        self._cache, self._sig = profs, sig
        return profs

    def get(self, emp):
        with self._lock:
            p = self._load().get(emp)
            return copy.deepcopy(p) if p else None

    def all(self):
        with self._lock:
            return copy.deepcopy(self._load())

    # ── 쓰기 ──────────────────────────────────────────────
    def _write(self, profs):
        os.makedirs(self.dir, exist_ok=True)
        body = json.dumps({"version": 1, "profiles": profs}, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        if os.path.exists(self.path):
            shutil.copyfile(self.path, self.path + ".prev")
            bdir = os.path.join(self.dir, "_backups")
            os.makedirs(bdir, exist_ok=True)
            daily = os.path.join(bdir, "profiles.%s.json" % datetime.date.today().strftime("%Y%m%d"))
            if not os.path.exists(daily):
                shutil.copyfile(self.path, daily)
        tmp = "%s.tmp.%d.%d" % (self.path, os.getpid(), threading.get_ident())
        # [SECURE] Unique temp file per process/thread then atomic replace - no half-written profile file (Category 3)
        try:
            with io.open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            for i in range(5):                      # [SECURE] Bounded retry - G: sync can hold the file briefly (Category 3)
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if i == 4:
                        raise
                    time.sleep(0.3 * (i + 1))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._cache, self._sig = None, None
        if self._parse(self.path) != profs:          # 다시 읽어 확인 — 쓴다는 것과 반영됐다는 것은 다르다
            raise RuntimeError("프로파일 저장이 반영되지 않았습니다")

    def _change(self, fn):
        with self._lock:
            with self._file_lock(create=True):
                profs = copy.deepcopy(self._load())
                out = fn(profs)
                self._write(profs)
                return copy.deepcopy(out)

    def create(self, emp, name, org, role, pin=None):
        def fn(profs):
            if emp in profs:
                raise Exists(emp)
            now = _now()
            rec = {"emp": emp, "name": name, "org": org, "role": role, "aliases": [], "ids": [],
                   "created": now, "updated": now, "last_login": now}
            p_clean = norm_pin(pin) if pin else ""
            if p_clean:
                rec["pin_salt"], rec["pin_hash"] = _hash_pin(p_clean)
            profs[emp] = rec
            return profs[emp]
        return self._change(fn)

    def update(self, emp, name, org, role):
        def fn(profs):
            p = profs.get(emp)
            if p is None:
                return None
            old = [p.get("name", ""), p.get("org", "")]
            if old != [name, org] and old not in (p.get("aliases") or []):
                p.setdefault("aliases", []).append(old)      # 예전 성함 · 소속으로 남긴 문의를 계속 잇는다
            p.update({"name": name, "org": org, "role": role, "updated": _now()})
            return p
        return self._change(fn)

    def set_pin(self, emp, pin):
        p_clean = norm_pin(pin)
        if not p_clean:
            raise ValueError("PIN 형식이 아닙니다(숫자 4~8자리)")
        def fn(profs):
            p = profs.get(emp)
            if p is None:
                return None
            p["pin_salt"], p["pin_hash"] = _hash_pin(p_clean)
            p["updated"] = _now()
            return p
        return self._change(fn)

    def clear_pin(self, emp):
        def fn(profs):
            p = profs.get(emp)
            if p is None:
                return None
            p.pop("pin_salt", None)
            p.pop("pin_hash", None)
            p["updated"] = _now()
            return p
        return self._change(fn)

    def link(self, emp, ids):
        def fn(profs):
            p = profs.get(emp)
            if p is None:
                return None
            cur = p.setdefault("ids", [])
            for i in ids:
                if i not in cur:
                    cur.append(i)
            del cur[:-MAX_IDS]
            return p
        return self._change(fn)

    def touch(self, emp):
        """마지막 로그인 — 하루에 한 번만 쓴다(드라이브에 자잘한 쓰기를 쌓지 않게)."""
        p = self.get(emp)
        if p and (p.get("last_login") or "")[:10] != _now()[:10]:
            def fn(profs):
                cur = profs.get(emp)
                if cur is not None:
                    cur["last_login"] = _now()
                return cur
            self._change(fn)

    def reset(self, emp):
        def fn(profs):
            return profs.pop(emp, None)
        return self._change(fn)


def main(argv):
    test = "--test" in argv
    argv = [a for a in argv if a != "--test"]
    st = Store(folder(test))
    print("저장 위치: %s" % st.path)
    if not argv or argv[0] == "list":
        for emp, p in sorted(st.all().items()):
            print("%-12s %s · %s · %s  (등록 %s · 최근 %s · 이어 둔 문의 %d%s)" % (
                emp, p.get("name"), p.get("org"), p.get("role"), p.get("created"), p.get("last_login"),
                len(p.get("ids") or []), " · PIN" if has_pin(p) else ""))
        return 0
    if len(argv) < 2:
        print("사번을 적어 주십시오.")
        return 2
    emp = norm_emp(argv[1])
    if not emp:
        print("사번 형식이 아닙니다(영문 · 숫자 · 하이픈 3~20자).")
        return 2
    if argv[0] == "show":
        rec = st.get(emp)
        if rec:
            rec = dict(rec)
            rec.pop("pin_hash", None)
            rec.pop("pin_salt", None)
            rec["pin_set"] = has_pin(st.get(emp))
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return 0
    if argv[0] == "clear-pin":
        rec = st.clear_pin(emp)
        print("PIN 을 해제했습니다: %s" % (public(rec) if rec else "등록되지 않은 사번입니다"))
        return 0
    if argv[0] == "reset":
        gone = st.reset(emp)
        print("지웠습니다(직전본은 profiles.json.prev): %s" % (public(gone) if gone else "그 사번은 등록돼 있지 않았습니다"))
        print("문의 기록은 그대로입니다. 같은 사번 · 성함 · 소속으로 다시 등록하면 예전 대화가 다시 보입니다.")
        return 0
    print("list · show · clear-pin · reset 중 하나입니다.")
    return 2


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))

# --------------------------------------------------
# Security Checklist
# Applied:
#   - Sensitive data location: profiles (employee id, name, org, hashed optional PIN) live outside the shared archive folder
#   - Session tokens never stored on disk; optional PIN stored only as PBKDF2-HMAC-SHA256 with random 16-byte salt
#   - Constant-time comparison: hmac.compare_digest used for optional PIN verification
#   - Race Condition: in-process mutex + cross-process OS file lock (profiles.json.lock), process/thread-unique temp file + atomic replace
#   - Integrity: .prev and daily backup before every write; re-read verification after write; corrupt file is set aside, never overwritten
#   - Input validation: employee id and optional PIN by whitelist regex; free text type-checked, control/bidi/middle-dot stripped, length capped
#   - Resource growth: linked inquiry ids capped per person
# Not Applied:
#   - [WARN] Missing Auth for Critical Function when user chooses not to set optional PIN (employee id alone identifies them on LAN)
#   - [WARN] File permissions: relies on the user's Google Drive folder permissions; no extra ACL is set
#   - [WARN] reset and clear-pin are destructive operator CLI commands; .prev and daily backups are kept without confirmation prompt
# --------------------------------------------------
