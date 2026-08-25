# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T09:44:25 | outage bắt đầu (SIGSTOP Region A, `netblock --mock`) | `chaos/chaos-events.jsonl:1` |
| 2026-08-25T09:44:25 (+0.0s) | user đầu tiên bị ảnh hưởng | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T09:44:44 (+19.2s) | health check alert (`UNHEALTHY`, region a) | `reports/health-events.jsonl:2` |
| 2026-08-25T09:44:51 (+25.7s) | operator/runbook confirm + DNS cutover sang B | `reports/failover-events.jsonl:5` |
| 2026-08-25T09:44:53 (+28.2s) | resolved (request đầu tiên OK từ region phụ) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.2s` · gap: `-271.8s` (đạt)
- RPO mục tiêu: 300s · đo được: `6.0s` (`3` doc bị mất) · gap: `-294.0s` (đạt)
- **Bước tốn nhiều giây nhất:** `health-check detection floor (19.2s, ~68% RTO)`.

Nguyên nhân chủ yếu là interval=5s × threshold=3, nên về lý thuyết health checker có thể mất khoảng 15s để phát hiện service bị lỗi. Ngoài ra còn có thời gian chờ tùy vào thời điểm outage xảy ra so với lần health check tiếp theo.

Vì vậy, trong lần đo này, thời gian phát hiện lỗi mới là phần chiếm nhiều thời gian nhất, chứ không phải snapshot, warm-up hay DNS như dự đoán ban đầu

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

1. Vì sao user chịu downtime 28.2s? — Vì hệ thống cần thời gian phát hiện outage rồi
   mới failover, không có gì tự phục hồi tức thời.
2. Vì sao phát hiện tốn tới 19.2s (68% RTO)? — Vì `dr/health_checker.py` poll mỗi 5s
   và chỉ chuyển `UNHEALTHY` sau 3 lần fail liên tiếp (chống flapping) → sàn lý thuyết
   15s, cộng thời gian chờ tới lượt poll tiếp theo kể từ lúc outage thật sự xảy ra.
3. Vì sao không chọn `interval`/`threshold` thấp hơn để phát hiện nhanh hơn? — Vì đó là
   đánh đổi trực tiếp: threshold thấp/interval thấp dễ biến 1 lần fail ngẫu nhiên
   (network blip <1s) thành "outage" giả, kích hoạt failover không cần thiết.
4. Vì sao hệ thống không tránh được đánh đổi tốc độ-vs-ổn định này? — Vì health-check
   dựa trên polling định kỳ về bản chất luôn phải chọn giữa "biết sớm" và "biết chắc";
   không có health check nào biết ngay lập tức mà vẫn miễn nhiễm false positive.
5. Vì sao đây không phải lỗi của một bước cụ thể trong runbook? — Đây là giới hạn thiết
   kế cố hữu của kiến trúc health-check-based failover (§4). Muốn giảm thật sự phải đổi
   kiến trúc (health check hai chiều, circuit breaker ở service mesh...), không phải
   "làm nhanh hơn" runbook hiện tại.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Hạ `interval` health check từ 5s xuống 3s (giữ threshold=3), đo tỷ lệ false-positive trên traffic thật trước khi chốt | SRE lead | +2 tuần | Giảm detect floor 15s→9s (~6s RTO) |
| 2 | Giữ pool region phụ ở trạng thái `full` thường trực (pre-warm) thay vì `warm→full` mỗi lần failover | Platform team | +1 tháng (cần review chi phí compute idle) | Giảm ~6.2s RTO (bỏ hẳn bước GPU warm-up) |
| 3 | Hạ `EDGE_TTL_SECONDS` từ 5s xuống 2s | Networking | +1 tuần | Giảm ~2-3s RTO phần DNS/LB cache |
| 4 | Tăng tần suất `state/replicate.py` từ 30s xuống 10s | Data team | +2 tuần | Giảm RPO trung bình từ ~6-15s xuống ~2-5s |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` = 5s × 3 = **15.0s** (sàn lý thuyết). Trên RTO đo được 28.2s,
   con số này chiếm 15.0/28.2 ≈ **53%**; thời gian phát hiện *thực đo* (19.2s, tính cả
   lệch pha poll) chiếm tới ≈ **68%** — health check vẫn là chi phí lớn nhất trong RTO.
2. Nếu hạ `interval` xuống 1s (giữ threshold=3): sàn lý thuyết còn 1×3=3s, tiết kiệm
   khoảng 12s RTO lý thuyết. Cái giá phải trả: `/readyz` bị gọi dồn dập gấp 5 lần, và với
   threshold vẫn = 3, một lần timeout ngẫu nhiên (network blip <1s) rơi đúng vào 3 lần
   poll liên tiếp trong 3s là hoàn toàn khả dĩ → dễ kích hoạt failover giả trong khi
   region chính vẫn khỏe, và nếu A hồi phục ngay sau đó thì hai region flap qua lại liên
   tục (§4 Anti-Patterns) — đúng thứ mà threshold ≥ 3 được thiết kế để chặn.
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn: `docs_lost` không còn
   là "vài document nằm trong khung 30s giữa 2 lần replicate" nữa, mà là **toàn bộ dữ
   liệu được ghi trong 6 giờ đó** — biến mất khỏi hệ thống vĩnh viễn, không thể khôi
   phục. Với khách hàng, đó là những giao dịch/hội thoại/hoá đơn thật sự "chưa từng tồn
   tại" theo góc nhìn của hệ thống sau failover. RPO đo bằng giây chỉ có ý nghĩa khi
   region chính còn khả năng phục hồi; khi mất vĩnh viễn, RPO chính là kích thước của
   "lỗ hổng dữ liệu không bao giờ lấy lại được".
