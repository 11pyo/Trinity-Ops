# -*- coding: utf-8 -*-
# SAMPLE: 이 시험의 문의 번호 · 사번 · 이름은 모두 합성 표본입니다.
"""
reproduce_defects.py — Argon 적대적 결함 재현 및 기준선/개선 측정 스크립트.

모든 시험은 임시 디렉터리(tempfile)와 임시 포트(127.0.0.1:0)에서만 실행한다.
운영 파일(5179, 5178, 5177, inquiry-log.js, _intake-*)에는 일절 손대지 않는다.
"""
import concurrent.futures
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_UI = os.path.dirname(HERE)
DASH = os.path.dirname(CHAT_UI)
DESK = os.path.join(DASH, "desk")

sys.path.insert(0, CHAT_UI)
sys.path.insert(0, DESK)


def header(title):
    print("\n=== %s ===" % title)


def test_multiline_read_log():
    header("1. multiline JSON in read_log() (desk-server & intake-server)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("desk_srv_test", os.path.join(DESK, "desk-server.py"))
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)

    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "inquiry-log.js")
        content = (
            'window.INQUIRY_LOG = window.INQUIRY_LOG || [];\n'
            'window.INQUIRY_LOG.push({"id": "INQ-261101-100001", "date": "2026-11-01", "type": "단순문의", "q": "단일줄 1", "by": "웹접수", "req": "홍길동·영업", "status": "접수"});\n'
            'window.INQUIRY_LOG.push({\n'
            '  "id": "INQ-261101-100002",\n'
            '  "date": "2026-11-01",\n'
            '  "type": "단순문의",\n'
            '  "q": "여러줄 1",\n'
            '  "by": "웹접수",\n'
            '  "req": "김철수·영업",\n'
            '  "status": "대기"\n'
            '});\n'
            'window.INQUIRY_LOG.push({\n'
            '  "id": "INQ-261101-100003",\n'
            '  "date": "2026-11-01",\n'
            '  "q": "여러줄 2",\n'
            '  "status": "접수"\n'
            '});\n'
            'window.INQUIRY_LOG.push({"id": "INQ-261101-100004", "date": "2026-11-01", "q": "단일줄 2", "status": "완료"});\n'
        )
        with io.open(log_path, "w", encoding="utf-8") as f:
            f.write(content)
        old_log = ds.LOG_JS
        ds.LOG_JS = log_path
        try:
            got = ds.read_log()
        finally:
            ds.LOG_JS = old_log
        print("expected=4, got=%d, ids=%s" % (len(got), sorted(got.keys())))
        return len(got) == 4


def test_heartbeat_crlf_and_race():
    header("2. heartbeat() CRLF byte offset drift & race condition (desk-server)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("desk_srv_hb", os.path.join(DESK, "desk-server.py"))
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)

    with tempfile.TemporaryDirectory() as td:
        hb_path = os.path.join(td, "heartbeat.jsonl")
        t0 = datetime_from_ts(1700000000)
        def mkline(dt, note):
            return json.dumps({"ts": dt.strftime("%Y-%m-%d %H:%M:%S"), "note": note}, ensure_ascii=False) + "\r\n"

        import datetime
        lines = [
            mkline(t0, "가나다라마바사"),
            mkline(t0 + datetime.timedelta(seconds=10), "아자차카타파하"),
        ]
        with io.open(hb_path, "wb") as f:
            f.write("".join(lines).encode("utf-8"))

        ds._hb.update({"path": "", "pos": 0, "last_sec": None, "last_ts": "", "gaps": [], "n": 0})
        ds.heartbeat(hb_path)
        pos_after_1 = ds._hb["pos"]
        actual_size_1 = os.path.getsize(hb_path)
        drift_1 = actual_size_1 - pos_after_1

        with io.open(hb_path, "ab") as f:
            f.write(mkline(t0 + datetime.timedelta(seconds=600), "끊김후복구").encode("utf-8"))
            f.write(mkline(t0 + datetime.timedelta(seconds=610), "정상").encode("utf-8"))
        age2, gaps2, n2 = ds.heartbeat(hb_path)
        pos_after_2 = ds._hb["pos"]
        actual_size_2 = os.path.getsize(hb_path)
        print("actual_size_1=%d pos_after_1=%d drift=%d | actual_size_2=%d pos_after_2=%d n_found=%d gaps=%d"
              % (actual_size_1, pos_after_1, drift_1, actual_size_2, pos_after_2, n2, len(gaps2)))
        return drift_1 == 0 and pos_after_2 == actual_size_2 and n2 == 4 and len(gaps2) == 1


def datetime_from_ts(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts)


def test_split_req_middledot():
    header("3. split_req() & clean() with middle dot (chat-server & accounts)")
    import accounts
    with tempfile.TemporaryDirectory() as td:
        store = accounts.Store(td)
        prof = store.create("E9001", accounts.clean("김·철수", 40), accounts.clean("영업·지원팀", 60), "영업대표")
        print("saved profile:", prof)
        sys.argv_bak = sys.argv[:]
        sys.argv = ["chat-server.py", "--test"]
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("chat_srv_t", os.path.join(CHAT_UI, "chat-server.py"))
            cs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cs)
            joined = cs.IX._who(prof["name"], prof["org"])
            split_n, split_o = cs.split_req(joined) if hasattr(cs, "split_req") else (prof["name"], prof["org"])
            match = (split_n == prof["name"] and split_o == prof["org"])
            print("joined=%r -> split=(%r, %r), roundtrip_ok=%s" % (joined, split_n, split_o, match))
            return match
        finally:
            sys.argv = sys.argv_bak


def test_accounts_keyerror_and_race():
    header("4. accounts.py KeyError after reset & concurrent save race")
    import accounts
    with tempfile.TemporaryDirectory() as td:
        store = accounts.Store(td)
        store.create("E1001", "테스터", "영업1팀", "영업대표")
        store.reset("E1001")
        keyerror_hit = False
        try:
            store.update("E1001", "테스터", "영업2팀", "영업대표")
        except KeyError as e:
            keyerror_hit = True
            print("KeyError caught on update after reset:", repr(e))
        except ValueError as e:
            print("Clean ValueError on update after reset:", e)

        try:
            store.touch("E1001")
        except KeyError as e:
            keyerror_hit = True
            print("KeyError caught on touch after reset:", repr(e))

        for i in range(10):
            store.create("T%03d" % i, "이름%d" % i, "조직", "영업대표")
        errors = []

        def worker(idx):
            try:
                for k in range(15):
                    store.update("T%03d" % idx, "이름%d" % idx, "조직%d" % k, "영업대표")
            except Exception as exc:
                errors.append(repr(exc))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        print("concurrent_errors=%s, keyerror_hit=%s" % (errors, keyerror_hit))
        return (not keyerror_hit) and (not errors)


def test_desk_req_score_crash():
    header("5. desk-server req_score non-int crash test")
    import importlib.util
    spec = importlib.util.spec_from_file_location("desk_srv_sc", os.path.join(DESK, "desk-server.py"))
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)

    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "inquiry-log.js")
        with io.open(log_path, "w", encoding="utf-8") as f:
            f.write('window.INQUIRY_LOG = [];\n')
            f.write('window.INQUIRY_LOG.push({"id": "INQ-261101-120000", "date": "2026-11-01", "type": "단순문의", "q": "테스트", "a": "[트리니티봇 · AI 1차 안내]\\n답변", "status": "완료", "req_closed": "2026-11-01 12:05", "req_score": "5점"});\n')
        old_log, old_test = ds.LOG_JS, ds.TEST
        ds.LOG_JS, ds.TEST = log_path, True
        crashed = False
        try:
            ov = ds.overview()
            print("overview ok, score_sum=%s" % ov["count"]["score_sum"])
        except Exception as e:
            crashed = True
            print("overview CRASHED with:", repr(e))
        finally:
            ds.LOG_JS, ds.TEST = old_log, old_test
        return not crashed


def test_chat_security_and_idempotency():
    header("6. chat-server Host/CSRF check & /api/new idempotency on retry")
    import importlib.util
    sys.argv_bak = sys.argv[:]
    sys.argv = ["chat-server.py", "--test"]
    with tempfile.TemporaryDirectory() as td:
        test_dash = os.path.join(td, "dash")
        os.makedirs(test_dash)
        shutil.copyfile(os.path.join(CHAT_UI, "_test", "intake-server.py"), os.path.join(test_dash, "intake-server.py"))
        shutil.copyfile(os.path.join(CHAT_UI, "_test", "log-inquiry.py"), os.path.join(test_dash, "log-inquiry.py"))
        with io.open(os.path.join(test_dash, "inquiry-log.js"), "w", encoding="utf-8") as f:
            f.write("window.INQUIRY_LOG = window.INQUIRY_LOG || [];\n")

        import accounts
        try:
            spec = importlib.util.spec_from_file_location("chat_srv_sec", os.path.join(CHAT_UI, "chat-server.py"))
            cs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cs)

            cs.IX.HERE = test_dash
            cs.IX.LOG_JS = os.path.join(test_dash, "inquiry-log.js")
            cs.IX.HELPER = os.path.join(test_dash, "log-inquiry.py")
            cs.IX.SHOT_DIR = os.path.join(td, "shots")
            cs.STORE = accounts.Store(td)
            cs.STORE.create("U100", "홍길동", "영업1팀", "영업대표")
            cs._sess.clear()

            srv = cs.IX.SingleServer(("127.0.0.1", 0), cs.Handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                def call(method, path, body=None, hdrs=None):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    h = dict(hdrs or {})
                    data = None
                    if body is not None:
                        data = json.dumps(body).encode("utf-8")
                        h.setdefault("Content-Type", "application/json")
                        h.setdefault("X-Requested-With", "SDChat")
                        h["Content-Length"] = str(len(data))
                    conn.request(method, path, body=data, headers=h)
                    resp = conn.getresponse()
                    raw = resp.read().decode("utf-8", errors="replace")
                    hdr_dict = {k.lower(): v for k, v in resp.getheaders()}
                    conn.close()
                    return resp.status, raw, hdr_dict

                sc_host, _, _ = call("GET", "/", hdrs={"Host": "evil.example:%d" % port})
                sc_csrf1, _, _ = call("POST", "/api/login", {"emp": "U100"}, hdrs={"Origin": "http://evil.example:%d" % port})
                # Missing X-Requested-With without Origin
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                b0 = json.dumps({"emp": "U100"}).encode("utf-8")
                conn.request("POST", "/api/login", body=b0, headers={"Content-Type": "application/json", "Content-Length": str(len(b0))})
                sc_csrf2 = conn.getresponse().status
                conn.close()

                sc_login, raw_login, hdr_login = call("POST", "/api/login", {"emp": "U100"})
                login_j = json.loads(raw_login)
                tok = login_j.get("tok", "")
                set_ck = hdr_login.get("set-cookie", "")
                ck_val = set_ck.split(";", 1)[0] if "sd_chat_tok=" in set_ck else ""
                csp_ok = "default-src 'self'" in hdr_login.get("content-security-policy", "")

                # Tiny valid 1x1 PNG base64 dict + cookie auth without tok in URL
                tiny_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                rid = "req-test-12345"
                _, r1, _ = call("POST", "/api/new", {"req_id": rid, "type": "단순문의", "q": "중복 테스트 문의\n[[사진:1]]", "shots": [{"data": tiny_b64}]},
                                hdrs={"Cookie": ck_val})
                _, r2, _ = call("POST", "/api/new", {"req_id": rid, "type": "단순문의", "q": "중복 테스트 문의\n[[사진:1]]", "shots": [{"data": tiny_b64}]},
                                hdrs={"Cookie": ck_val})
                j1, j2 = json.loads(r1), json.loads(r2)
                id1, id2 = j1.get("id"), j2.get("id")
                sc_shot, _, hdr_shot = call("GET", "/api/shot?id=%s&n=1" % id1, hdrs={"Cookie": ck_val})

                # Test optional PIN & wrong PIN throttling
                sc_pin_set, r_pin_set, _ = call("POST", "/api/pin", {"new_pin": "2468"}, hdrs={"Cookie": ck_val})
                _, r_need_pin, _ = call("POST", "/api/login", {"emp": "U100"})
                _, r_wrong_pin, _ = call("POST", "/api/login", {"emp": "U100", "pin": "0000"})
                _, r_ok_pin, _ = call("POST", "/api/login", {"emp": "U100", "pin": "2468"})
                _, r_pin_clr, _ = call("POST", "/api/pin", {"cur_pin": "2468", "new_pin": ""}, hdrs={"Cookie": ck_val})
                pin_ok = (json.loads(r_pin_set).get("profile", {}).get("pin_set") is True
                          and json.loads(r_need_pin).get("need_pin") is True
                          and json.loads(r_wrong_pin).get("ok") is False
                          and json.loads(r_ok_pin).get("ok") is True
                          and json.loads(r_pin_clr).get("profile", {}).get("pin_set") is False)

                # Test 20 concurrent pollers + 5 concurrent writers (2 new + 3 profile updates, respecting per-IP NEW_MAX=3)
                conc_errors = []
                def do_poll(i):
                    st, body, _ = call("GET", "/api/pulse", hdrs={"Cookie": ck_val})
                    if st != 200 or not json.loads(body).get("ok"):
                        conc_errors.append("poll:%d:%s" % (i, st))
                def do_write(i):
                    if i < 2:
                        st, body, _ = call("POST", "/api/new", {"req_id": "conc-wr-%04d" % i, "type": "단순문의", "q": "동시성 테스트 %d" % i},
                                           hdrs={"Cookie": ck_val})
                    else:
                        st, body, _ = call("POST", "/api/profile", {"name": "홍길동", "org": "영업%d팀" % i, "role": "영업대표"},
                                           hdrs={"Cookie": ck_val})
                    if st != 200 or not json.loads(body).get("ok"):
                        conc_errors.append("write:%d:%s:%s" % (i, st, body[:120]))
                with concurrent.futures.ThreadPoolExecutor(max_workers=25) as ex:
                    futs = [ex.submit(do_poll, i) for i in range(20)] + [ex.submit(do_write, i) for i in range(5)]
                    for fu in futs:
                        fu.result()

                print("host=403:%s csrf=%s/%s csp:%s cookie_shot:%s dedup:(%s==%s) pin:%s conc_err:%s"
                      % (sc_host == 403, sc_csrf1, sc_csrf2, csp_ok, sc_shot, id1, id2, pin_ok, conc_errors))
                return (sc_host == 403 and sc_csrf1 == 400 and sc_csrf2 == 400 and csp_ok
                        and sc_shot == 200 and id1 is not None and id1 == id2 and pin_ok and not conc_errors)
            finally:
                srv.shutdown()
                srv.server_close()
        finally:
            sys.argv = sys.argv_bak


def test_gap_clustering_quality():
    header("7. gap clustering quality on Korean SD questions")
    import importlib.util
    spec = importlib.util.spec_from_file_location("desk_srv_gap", os.path.join(DESK, "desk-server.py"))
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)

    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "inquiry-log.js")
        rows = [
            {"id": "INQ-261101-100001", "date": "2026-11-01", "status": "대기", "q": "VA01 저장 오류 납품처 누락", "a": "[트리니티봇 · 접수 확인]\n보류"},
            {"id": "INQ-261102-100002", "date": "2026-11-02", "status": "대기", "q": "판매오더 생성 시 저장이 안 됩니다 (VA01)", "a": "[트리니티봇 · 접수 확인]\n보류"},
            {"id": "INQ-261103-100003", "date": "2026-11-03", "status": "대기", "q": "출하 현황 어디서 봐요?", "a": "[트리니티봇 · 접수 확인]\n보류"},
            {"id": "INQ-261104-100004", "date": "2026-11-04", "status": "대기", "q": "반품 현황 어디서 봐요?", "a": "[트리니티봇 · 접수 확인]\n보류"},
        ]
        with io.open(log_path, "w", encoding="utf-8") as f:
            f.write("window.INQUIRY_LOG = [];\n")
            for r in rows:
                f.write("window.INQUIRY_LOG.push(" + json.dumps(r, ensure_ascii=False) + ");\n")
        old_log, old_test = ds.LOG_JS, ds.TEST
        ds.LOG_JS, ds.TEST = log_path, True
        try:
            g = ds.overview()["gaps"]
        finally:
            ds.LOG_JS, ds.TEST = old_log, old_test
        print("gap clusters:", [(x["q"], x["n"]) for x in g])
        # Check if VA01 (or n=2) formed and 출하/반품 didn't falsely merge into one n=2 cluster
        va01_merged = any(x["n"] >= 2 and ("VA01" in x["q"] or "판매오더" in x["q"]) for x in g)
        ship_ret_merged = any(x["n"] >= 2 and ("출하 현황" in x["q"] or "반품 현황" in x["q"]) for x in g)
        print("va01_merged=%s, ship_ret_falsely_merged=%s" % (va01_merged, ship_ret_merged))
        return va01_merged and not ship_ret_merged


def test_scale_baseline():
    header("8. Scale & concurrency baseline (10k log, 600k heartbeats, 1k profiles)")
    import importlib.util, datetime
    spec = importlib.util.spec_from_file_location("desk_srv_sc2", os.path.join(DESK, "desk-server.py"))
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)

    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "inquiry-log.js")
        hb_path = os.path.join(td, "heartbeat.jsonl")

        t0 = time.perf_counter()
        with io.open(log_path, "w", encoding="utf-8") as f:
            f.write("window.INQUIRY_LOG = [];\n")
            for i in range(2500):
                iid = "INQ-261101-%06d" % i
                f.write('window.INQUIRY_LOG.push(' + json.dumps({
                    "id": iid, "date": "2026-11-01", "type": "단순문의",
                    "q": "VL01N 출고 처리 중 여신 한도 초과 메시지 %d" % (i % 50),
                    "by": "웹접수", "req": "사용자%d·영업%d팀" % (i % 200, i % 10), "status": "접수"
                }, ensure_ascii=False) + ');\n')
                f.write('window.INQUIRY_LOG.push(' + json.dumps({
                    "id": iid, "status": "대기",
                    "a": "[트리니티봇 · AI 1차 안내 · 담당자 부재중]\nVL01N 확인 안내 본문입니다.\n⏱2026-11-01 10:00"
                }, ensure_ascii=False) + ');\n')
                f.write('window.INQUIRY_LOG.push(' + json.dumps({
                    "id": iid, "status": "완료", "req_closed": "2026-11-01 10:15", "req_score": 5
                }, ensure_ascii=False) + ');\n')
                f.write('window.INQUIRY_LOG.push(' + json.dumps({
                    "id": iid, "botgrade": "맞음", "botgrade_at": "2026-11-02 09:00"
                }, ensure_ascii=False) + ');\n')
        print("generated 10k log lines in %.2fs (size=%.1f MB)" % (time.perf_counter() - t0, os.path.getsize(log_path) / (1024*1024)))

        t0 = time.perf_counter()
        base_dt = datetime.datetime(2026, 11, 1, 0, 0, 0)
        with io.open(hb_path, "wb") as f:
            lines = []
            for i in range(1000):
                dt = base_dt + datetime.timedelta(seconds=i * 5)
                lines.append(json.dumps({"ts": dt.strftime("%Y-%m-%d %H:%M:%S")}))
            bchunk = ("\n".join(lines) + "\n").encode("utf-8")
            for _ in range(600):
                f.write(bchunk)
        print("generated 600k heartbeats in %.2fs (size=%.1f MB)" % (time.perf_counter() - t0, os.path.getsize(hb_path) / (1024*1024)))

        old_log = ds.LOG_JS
        ds.LOG_JS = log_path
        ds._hb.update({"path": "", "pos": 0, "last_sec": None, "last_ts": "", "gaps": [], "n": 0})
        try:
            t1 = time.perf_counter()
            ds.read_log_cached() if hasattr(ds, "read_log_cached") else ds.read_log()
            t_read1 = (time.perf_counter() - t1) * 1000
            t2 = time.perf_counter()
            ds.read_log_cached() if hasattr(ds, "read_log_cached") else ds.read_log()
            t_read2 = (time.perf_counter() - t2) * 1000

            t3 = time.perf_counter()
            ds.heartbeat(hb_path)
            t_hb1 = (time.perf_counter() - t3) * 1000
            t4 = time.perf_counter()
            ds.heartbeat(hb_path)
            t_hb2 = (time.perf_counter() - t4) * 1000
        finally:
            ds.LOG_JS = old_log

        import accounts
        store = accounts.Store(td)
        profs = {}
        for i in range(1000):
            profs["E%04d" % i] = {"emp": "E%04d" % i, "name": "이름%d" % i, "org": "영업%d팀" % (i % 20), "role": "영업대표", "aliases": [], "ids": []}
        with io.open(store.path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "profiles": profs}, f, ensure_ascii=False)
        t5 = time.perf_counter()
        for i in range(50):
            store.get("E%04d" % i)
        t_prof_get50 = (time.perf_counter() - t5) * 1000

        print("read_log cold=%.1fms warm=%.1fms | hb_600k cold=%.1fms warm=%.1fms | 50x_profile_get(1k)=%.1fms"
              % (t_read1, t_read2, t_hb1, t_hb2, t_prof_get50))
    return True


if __name__ == "__main__":
    results = {}
    for name, fn in [
        ("multiline_read_log", test_multiline_read_log),
        ("heartbeat_crlf_race", test_heartbeat_crlf_and_race),
        ("split_req_middledot", test_split_req_middledot),
        ("accounts_keyerror_race", test_accounts_keyerror_and_race),
        ("desk_req_score_crash", test_desk_req_score_crash),
        ("chat_sec_and_dedup", test_chat_security_and_idempotency),
        ("gap_clustering", test_gap_clustering_quality),
        ("scale_baseline", test_scale_baseline),
    ]:
        try:
            results[name] = bool(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            results[name] = False
    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print("  %-26s : %s" % (k, "PASS" if v else "FAIL (DEFECT REPRODUCED)"))
