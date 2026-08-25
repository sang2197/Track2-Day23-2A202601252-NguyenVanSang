"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True (dùng cho CI/chấm điểm); ngược lại hỏi y/N thật."""
    if auto:
        return True
    ans = input(f"{msg} [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def _confirm_outage(primary: str, target: str, wait_for_health: float = 45.0, poll: float = 0.5):
    """Ưu tiên đợi dr/health_checker.py (chạy song song, watchdog riêng) tự phát hiện
    `primary` UNHEALTHY trong reports/health-events.jsonl -- KHÔNG tự probe nhanh rồi
    cutover trước khi health checker kịp bắt: t_cutover < t_detect bị
    tools/measure_rto.py đánh dấu INVALID (xem RUBRIC.md).
    Nếu không thấy tín hiệu nào trong wait_for_health giây (không có health checker nào
    đang chạy), fallback: tự probe primary threshold=3 lần liên tiếp.
    """
    health_path = pathlib.Path("reports/health-events.jsonl")
    seen = set(health_path.read_text().splitlines()) if health_path.exists() else set()
    target_probe = hc.probe(target, timeout=2.0)
    deadline = time.time() + wait_for_health
    while time.time() < deadline:
        if health_path.exists():
            for line in health_path.read_text().splitlines():
                if not line.strip() or line in seen:
                    continue
                seen.add(line)
                e = json.loads(line)
                if (e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                        and e.get("region") == primary):
                    return True, "health_checker", e, target_probe
        time.sleep(poll)
    probes = []
    for _ in range(3):
        probes.append(hc.probe(primary, timeout=2.0))
        time.sleep(0.5)
    confirmed = all(not ok for ok, _ in probes)
    return confirmed, "direct_probe_fallback", probes, target_probe


def _last_outage_ts(primary: str):
    p = pathlib.Path("chaos/chaos-events.jsonl")
    if not p.exists():
        return None
    events = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    kills = [e for e in events if e.get("action") == "kill" and e.get("region") == primary]
    return kills[-1]["ts"] if kills else None


def _golden_signals(target: str, n_requests: int = 10):
    """10 request thật thẳng vào region phụ (không qua edge, tránh nhiễu bởi TTL cache)."""
    latencies, errors = [], 0
    for i in range(n_requests):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", timeout=5,
                          params={"q": f"hoa don thang {i % 12 + 1}"})
            ok = r.status_code == 200
        except Exception:
            ok = False
        latencies.append((time.time() - t0) * 1000)
        if not ok:
            errors += 1
    latencies.sort()
    idx = max(0, int(len(latencies) * 0.95) - 1)
    p95 = round(latencies[idx], 1) if latencies else None
    return {"n_requests": n_requests, "error_rate": round(errors / n_requests, 2),
            "p95_latency_ms": p95}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    # 1. xac_nhan_outage -- chờ health_checker thật phát hiện, không tự probe nhanh qua mặt
    confirmed, method, detail, target_probe = _confirm_outage(primary, target)
    step(1, "xac_nhan_outage", primary=primary, target=target, method=method,
         confirmed_outage=confirmed, detail=detail, target_probe=target_probe)

    # 2. thong_bao_incident
    t_outage = _last_outage_ts(primary)
    t_announce = time.time()
    step(2, "thong_bao_incident", primary=primary, t_outage=t_outage, t_announce=t_announce,
         notify_delay_s=None if t_outage is None else round(t_announce - t_outage, 2))

    if not confirm(auto, f"Region {primary} down (confirmed_outage={confirmed}). "
                         f"Failover sang region {target}?"):
        step(3, "scale_gpu_pool", skipped=True, reason="operator khong confirm")
        return {"ok": False, "reason": "not_confirmed", "confirmed_outage": confirmed}

    # 3. scale_gpu_pool -- gọi failover.failover(...) MỘT LẦN DUY NHẤT
    fo_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, backend=backend, ok=fo_result.get("ok"),
         reason=fo_result.get("reason"))

    # 4. verify_state_replica -- chỉ ĐỌC lại kết quả bước 3, không gọi lại failover
    step(4, "verify_state_replica", ok=fo_result.get("ok"),
         state=fo_result.get("target_state_after"),
         rpo_seconds=fo_result.get("rpo_seconds"), docs_lost=fo_result.get("docs_lost"))

    # 5. dns_cutover -- cũng chỉ đọc lại
    step(5, "dns_cutover", ok=fo_result.get("ok"),
         active_region=target if fo_result.get("ok") else None)

    # 6. verify_golden_signals
    golden = _golden_signals(target) if fo_result.get("ok") else \
        {"n_requests": 0, "error_rate": None, "p95_latency_ms": None, "skipped": True}
    step(6, "verify_golden_signals", **golden)

    # 7. post_incident
    elapsed = None if t_outage is None else round(time.time() - t_outage, 1)
    step(7, "post_incident", elapsed_s=elapsed,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")

    return {"ok": fo_result.get("ok"), "confirmed_outage": confirmed, "failover": fo_result,
            "golden_signals": golden, "elapsed_s": elapsed}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
