# -*- coding: utf-8 -*-
"""
build_test.py — 시험 사본을 운영본과 같게 맞춘다 (운영 서버 · 운영 로그는 건드리지 않는다)

    python task-dashboard/chat-ui/build_test.py
    python -B task-dashboard/chat-ui/chat-server.py --test        → http://<이 PC>:5183/

`_test/` 에 운영 `intake-server.py` · `log-inquiry.py` 를 **고치지 않고 그대로** 복사한다.
chat-server.py --test 는 이 사본을 모듈로 불러 쓰므로, 업무 규칙(접수 · 추가 질문 · 닫기 · 그림 · 상태 이름)은 운영과 같고
기록은 `_test/inquiry-log.js`(시험 로그)에만 남는다. 시험 로그 · 시험 프로파일은 지우지 않는다.

운영 접수 서버를 고친 뒤에는 이 스크립트를 다시 돌려 시험 사본을 따라가게 한다.
"""
import filecmp
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
OUT = os.path.join(HERE, "_test")
FILES = ("intake-server.py", "log-inquiry.py")


def main():
    os.makedirs(OUT, exist_ok=True)
    for fn in FILES:
        src, dst = os.path.join(DASH, fn), os.path.join(OUT, fn)
        shutil.copyfile(src, dst)
        if not filecmp.cmp(src, dst, shallow=False):          # 복사본이 운영본과 바이트까지 같은지
            raise SystemExit("[X] %s 복사본이 운영본과 다릅니다" % fn)
        print("  같게 맞춤: %s" % fn)
    old = os.path.join(OUT, "chat-page.built.html")            # 옛 방식(화면을 끼워 넣은 사본)의 잔재
    if os.path.exists(old):
        os.remove(old)
    print("시험 로그: %s (그대로 둠)" % os.path.join(OUT, "inquiry-log.js"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# --------------------------------------------------
# Security Checklist
# Applied:
#   - Path Traversal: fixed file names and paths derived from this file's location
#   - Integrity: byte-for-byte comparison after copy
#   - Data separation: test copies write only to the test log under _test/
# Not Applied:
#   - [WARN] None specific to this script; the test server it prepares is LAN-exposed — stop it after testing
# --------------------------------------------------
