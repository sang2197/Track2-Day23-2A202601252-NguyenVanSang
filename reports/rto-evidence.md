# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T09:48:23` | chaos kill | `chaos/chaos-events.jsonl:4` |
| Request fail đầu tiên | `+0.0s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:1` |
| User thấy lỗi đầu tiên | 0.0s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | 19.2s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 19.5s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 25.7s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 25.7s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | 28.2s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `28.2s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `6.0s` / `3` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 19.2 | Sàn lý thuyết `interval_s(5) × threshold(3) = 15.0s`, thực đo lệch pha poll thêm ~4.2s tại `reports/health-events.jsonl:2` | Hạ `interval`/`threshold` → phát hiện nhanh hơn nhưng dễ bắt nhầm 1 lần fail ngẫu nhiên thành outage thật (flapping, §4 Anti-Patterns) |
| Snapshot restore + scale pool | 0.3 | `2_restore_snapshot` (`reports/failover-events.jsonl:2`) → `3_scale_pool` (`reports/failover-events.jsonl:3`), backend `fs` copy 2 file nhỏ | Đã gần tối thiểu ở quy mô lab; ở quy mô thật (S3/MinIO, snapshot lớn) bước này sẽ chiếm tỷ trọng lớn hơn nhiều |
| GPU pool warm-up | 6.2 | `3_scale_pool` → `4_wait_ready` (`reports/failover-events.jsonl:3` → `:4`), đúng bằng `WARMUP_SECONDS=6` mặc định ở `scripts/up_bare.sh` | Giữ region phụ ở pool `full` thường trực (pre-warm) thay vì `warm→full` mỗi lần failover — đổi lại tốn chi phí compute chờ sẵn liên tục |
| DNS/LB TTL cache | 2.4 | `reports/drill-2-withdr.jsonl:39` (request thành công) trừ `5_dns_cutover` (`reports/failover-events.jsonl:5`) | Hạ `EDGE_TTL_SECONDS` (hiện 5s) → cutover phản ánh nhanh hơn, đổi lại edge phải đọc file `active_region` mỗi request thường xuyên hơn |

Tổng 4 thành phần: 19.2 + 0.3 + 6.2 + 2.4 = 28.1s ≈ 28.2s đo được (chênh lệch 0.1s do làm tròn từng mốc).
