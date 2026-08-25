# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | `a.alive=false` (hoặc `a.ready=false`) 3 lần liên tiếp, cách nhau vài giây | on-call |
| 2 | Mở incident, bấm giờ, kích hoạt failover | `python3 dr/runbook.py --primary a --target b --backend fs` (bỏ `--auto` để runbook tự hỏi xác nhận `y/N` trước khi đổi gì) | dòng `step:1, name:xac_nhan_outage` xuất hiện trong `reports/runbook-run.jsonl` | on-call |
| 3 | Restore state ở region phụ | *(tự động — nằm trong lệnh ở bước 2)* | dòng `step:2_restore_snapshot` trong `reports/failover-events.jsonl` có `rpo_seconds` và `docs_lost` khác `null` | hệ thống (`dr/failover.py`) |
| 4 | Scale pool warm→full | *(tự động)* | dòng `step:4_wait_ready, ready:true` trong `reports/failover-events.jsonl` | hệ thống (`dr/failover.py`) |
| 5 | DNS/LB cutover | *(tự động — CHỈ chạy nếu bước 4 `ready:true`)* | `curl localhost:8080/edge/state` trả `active_region:"b"` VÀ dòng `step:5_dns_cutover` trong `reports/failover-events.jsonl` | hệ thống (`dr/failover.py`) |
| 6 | Verify golden signals | *(tự động — 10 request thật thẳng vào region b)* | dòng `step:6, name:verify_golden_signals` trong `reports/runbook-run.jsonl`: `error_rate <= 0.1` và `p95_latency_ms < 500` | on-call |
| 7 | Đo RTO + viết postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` = `"PASS"` (không phải `null`/`"FAIL"`), `valid:true`, `warnings:[]` | on-call |

**Rollback (failover ngược):** chỉ trả traffic về Region A khi **cả ba** điều kiện sau
đều đúng: (1) root cause khiến A down đã được xác định và fix xong — không chỉ "A lại
`/healthz` 200" một lần; (2) A pass `/readyz` ổn định liên tục ≥ 10 phút (không phải 1
lần đúng); (3) dữ liệu mới ghi vào B trong lúc B làm primary đã được replicate ngược về
A, để không lặp lại một lần mất dữ liệu nữa khi quay lại A. Người có quyền quyết định:
**on-call lead / incident commander của ca trực đó** — KHÔNG chạy full-auto (§4
Anti-Patterns: failover 2 chiều không có circuit breaker gây flapping liên tục giữa 2
region).
