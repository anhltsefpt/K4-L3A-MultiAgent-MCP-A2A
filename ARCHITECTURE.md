# L3A Architecture Record

Framework: thuần Python `asyncio` state-machine (không phụ thuộc framework agent). Điểm vào duy nhất là `solve_case()` trong `src/student_agent/workflow.py`; state contract nằm ở `src/student_agent/state.py`.

## 1. System overview

```text
inputs/<case_id>.json
        │  (CLI emits case_received)
        ▼
  Coordinator ──task_assigned──► Order agent ────┐
        │                        Payment agent ──┼─ MCP Gateway (Bearer team key, case_id trên mọi call)
        │                        Shipment agent ─┘        │
        │                              │ handoff          │ tool_result_consumed
        │                              ▼                  ▼
        │                        Policy agent ◄──── get_policy
        │                              │ handoff
        │                              ▼
        │                        Verifier agent  (không gọi tool)
        │                              │ verification_completed
        ▼                              ▼
   Output (schema-validated) ── case_finalized (CLI) ──► outputs/<case_id>.json + traces/trace.jsonl
```

Specialist chạy đồng thời (`asyncio.gather`) trên cùng một `CaseState`, nhưng mọi MCP call đi qua một `asyncio.Lock` nên chỉ có 1 call trong bay tại một thời điểm (session MCP không chịu được burst, xem mục 5). Policy chỉ chạy sau khi cả ba specialist đã handoff.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case JSON | Dựng `CaseState`, phát `task_assigned` cho từng specialist, điều phối thứ tự, gom kết quả cuối | Task → specialists; kết quả Verifier → output |
| Order/item | `order_id` | Trạng thái đơn, timestamp, item, seller_id, giá/freight | `Finding` (signals: `order_status`, `items`) → Policy |
| Payment | `order_id` | Payment rows + lifecycle events (capture/mismatch) từ `get_payment_timeline`, refund events từ `get_refund_timeline` | `Finding` (signals: `payments`, `payment_events`, `refund_events`) → Policy |
| Shipment | `order_id` | Mốc giao hàng, shipping limit, event `delivered_late` + `actor`, thông tin seller | `Finding` (signals: `delivery`, `late_actor`, `seller`) → Policy |
| Policy | 3 `Finding` + policy MCP | Chọn `primary_issue`, `case_status`, bên chịu trách nhiệm, refund lines, actions, claim assessments | `Decision` → Verifier |
| Verifier | `Decision` + evidence | Kiểm tra bất biến, hiệu chuẩn `confidence`, validate schema | Output đã xác thực (hoặc lỗi trả Policy tối đa 1 lần) |

Quyền gọi tool (least privilege). Ma trận `AGENT_TOOLS` đã định nghĩa trong `state.py`; lớp gateway theo agent (Pha 3) sẽ cưỡng chế bằng cách ném `ToolNotPermitted` khi agent gọi tool ngoài danh sách, đồng thời tự gắn `case_id`:

| Actor | Tool được phép |
| --- | --- |
| Order/item | `get_order`, `get_order_items` |
| Payment | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` (thực tế chỉ gọi timeline + refund) |
| Shipment | `get_shipment_summary`, `get_sellers` |
| Policy | `get_policy` |
| Coordinator, Verifier | không có |

`get_payment_timeline` là superset của `get_order_payments` (payments + events), nên `get_order_payments` được giao quyền nhưng không gọi để tránh evidence trùng lặp domain. `get_sellers` chỉ được Shipment agent gọi khi có event giao trễ do `seller`. `get_refund_timeline` luôn được gọi ở chế độ optional. `get_product_context` và `get_customer_history` cố ý không giao cho agent nào: không phục vụ 10 loại issue của L3A và chỉ làm giảm điểm evidence relevance (forbidden-domain penalty). Tên tool lấy từ tool discovery (`day09 mcp-tools`), ví dụ tool thật là `get_order_payments`, `get_shipment_summary`, `get_sellers`, không phải `get_payment/get_shipment/get_seller`.

## 3. A2A protocol

- **Envelope** (`A2AMessage`): `case_id`, `message_id`, `sender`, `recipient`, `kind` ∈ {`task`, `result`, `handoff`}, `body`, `evidence_refs`. Correlation theo `case_id` (một `CaseState` cho mỗi case, không chia sẻ giữa case).
- **Điều kiện handoff**: specialist handoff sang Policy khi đã có `Finding` (kể cả `missing` không rỗng); Policy handoff sang Verifier khi đã có `Decision`. Mỗi handoff phát event `handoff` với `actor` = bên gửi, `target` = bên nhận.
- **Timeout**: mỗi MCP call bị giới hạn bằng `asyncio.wait_for`; hết hạn được xử lý theo mục 5.
- **Chống vòng lặp**: đồ thị một chiều Coordinator → Specialists → Policy → Verifier. Verifier chỉ được trả lại Policy đúng 1 lần; lần thứ hai không đạt thì hạ xuống `insufficient_evidence` / `needs_investigation` thay vì lặp.
- Trace chỉ chứa event và `decision_code` quan sát được, không chứa prompt hay suy luận riêng.

## 4. Evidence lifecycle

1. Agent gọi `gateway.call(tool, case_id=..., ...)`. `EvidenceGateway` validate envelope theo `mcp-evidence-response-v1` ngay khi nhận.
2. `CaseState.record()` lưu nguyên vẹn `evidence_ref`, `result_hash`, `domain`, `data`. **Không bao giờ tự sinh hoặc sửa `evidence_ref`.**
3. Agent rút ra `Finding` và ghi entity id (order/item/seller/payment/shipment) **từ evidence**, không từ lời khai của khách.
4. Ngay sau khi tiêu thụ evidence, agent phát `tool_result_consumed` với `tool_name` và `evidence_refs=[ref]`.
5. Policy/Verifier chỉ đưa vào `evidence_refs` của output (và của từng `claim_assessment`) những ref thực sự hỗ trợ kết luận đó.
6. `CaseState` tạo riêng cho từng case nên evidence không bị dùng lại giữa các case; server cũng từ chối `case_id` không khớp scope (kiểm chứng: gọi order của case này dưới `case_id` khác trả lỗi).

## 5. Failure policy

Lỗi MCP trả về thông báo chung (`Error executing tool <name>`), không phân biệt được "không có dữ liệu" và "sai scope", nên xử lý theo tool chứ không dựa vào nội dung lỗi.

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| Session MCP chết giữa batch (ConnectError / cancelled scope; quan sát thực tế sau ~10–40 call) | Có: retry **cả case** trên session mới, tối đa 4 lần, backoff 1s/2s/3s; trace của lần hỏng bị rollback (không sinh event trùng). Chỉ retry khi mọi exception lá đều là lỗi transport; lỗi logic không retry | Hết lượt: `day09 run` dừng và báo rõ case lỗi, không sinh output giả | (không có event; trace lần hỏng bị hủy) |
| MCP call timeout (60s) | Như trên: coi là lỗi session | như trên | như trên |
| Not found / lỗi tool cố định (`RuntimeError` từ tool) | Retry 1 lần rồi dừng. Riêng `get_refund_timeline` (optional): không retry, lỗi = không có refund event (quan sát thực tế ở case không có refund) | Không suy đoán dữ liệu; tool bắt buộc thất bại được ghi vào `Finding.missing` | `handoff` với `decision_code=findings_partial`, `attributes.missing=<tool>` |
| Source conflict (ví dụ order `canceled` nhưng có event `delivered_late`) | Không | Chọn nguồn theo thứ tự ưu tiên (order/timeline có thẩm quyền > payment row > lời khai), ghi vào `data_conflicts`, giảm confidence | `decision_code=source_conflict` |
| Invalid specialist result | Không retry gọi tool; Verifier trả lại Policy 1 lần | Nếu vẫn sai: `insufficient_evidence`, `needs_investigation`, confidence thấp | `decision_code=specialist_invalid` |

Retry có giới hạn cứng, không vòng lặp vô hạn. Missing evidence không bao giờ bị chuyển thành dữ liệu phỏng đoán.

## 6. Verification invariants

Verifier kiểm tra trước khi finalize:

1. **Schema**: output pass `l3a-output-v2.schema.json`, không có field ngoài schema.
2. **Entity scope**: `order_ids` chỉ chứa order xuất hiện trong evidence của chính case; `case_id` khớp input.
3. **Evidence ownership**: mọi `evidence_ref` trong output đều có trong `CaseState.evidence`; không có ref tự tạo.
4. **Claim linkage**: mỗi `claim_assessment` chỉ trích dẫn ref thuộc domain hỗ trợ verdict của nó.
5. **Money totals**: `recommended_refund_brl` = tổng `refund_lines[].amount_brl` (làm tròn 2 chữ số); `no_action` ⇒ refund = 0.
6. **Responsibility/action consistency**: `primary_issue` ↔ `case_status` ↔ `responsible_parties` ↔ `resolution_actions` khớp bảng luật của `get_policy` (ví dụ lỗi do seller thì không gán logistics_provider chịu hoàn tiền; `no_action` không có action hoàn tiền; action không trùng lặp).
7. **Confidence bounds**: `confidence` ∈ [0, 1]; bị chặn trên (< 1.0) khi có `data_conflicts` hoặc `Finding.missing`; thấp cho `insufficient_evidence`.
8. **Trace lifecycle**: đủ `case_received → task_assigned → tool_result_consumed → handoff → policy_decided → verification_completed → case_finalized`, đúng thứ tự receive/finalize.

### Quyết định của Policy agent (`policy.py`)

1. Mỗi issue có một điều kiện dựa trên evidence (ví dụ `canceled_order_paid` cần `order_status=canceled` và có payment `captured`; `late_delivery_*` cần đơn `delivered`, giao muộn theo timestamp **và** event `delivered_late` do đúng actor; `duplicate_charge` cần cặp payment trùng và không phải split hợp lệ voucher+thẻ; `payment_mismatch` cần `reconciliation_mismatch` đang `open`; `refund_pending/failed` theo trạng thái refund event).
2. Tập issue được evidence hỗ trợ (`supported`) có thể nhiều hơn một (dữ liệu chứa cả tín hiệu phụ). Lời khai của khách **chỉ** dùng để chọn giữa các issue đã được evidence hỗ trợ (`claim_confirmed` / `claim_tiebreak`). Nếu lời khai bị bác bỏ mà evidence hỗ trợ issue khác thì chọn issue theo evidence (`evidence_only`); nếu không issue nào được hỗ trợ thì `unsupported_claim` (`no_support`).
3. `case_status`, action, số tiền hoàn và loại bên chịu trách nhiệm lấy từ bảng luật `get_policy`. `party_id` của seller là seller thật gắn với đơn (từ evidence), không dùng id mẫu trong policy.
4. Timestamp của các dòng bất thường trong dữ liệu là nhiễu; chỉ event có `status` và `actor` mới được coi là tín hiệu. Mâu thuẫn nguồn (đơn `canceled` nhưng có event giao muộn; event giao muộn nhưng timestamp giao đúng hạn) được ghi vào `data_conflicts` và nguồn có thẩm quyền được chọn.
5. Thiếu evidence lõi (`get_order`, `get_payment_timeline`, `get_shipment_summary`, `get_policy`) hoặc không có luật cho issue: `insufficient_evidence`, `needs_investigation`, hoàn 0.

### Hiệu chuẩn confidence (`verifier.py`)

| Cơ sở chọn issue | Confidence gốc |
| --- | --- |
| `claim_confirmed` (chỉ issue được khai có evidence) | 0.92 |
| `claim_tiebreak` / `no_support` | 0.80 |
| `evidence_only` (lời khai bị bác bỏ) | 0.70 |
| `insufficient_evidence` | 0.25 |

Trừ 0.05 cho mỗi `data_conflict` và chặn trên 0.90 khi có conflict; tuyệt đối không vượt 0.95. Claim `requested_full_refund` bị chặn trên 0.85. Verifier không tự sửa số liệu: khi phát hiện vi phạm nó hạ xuống `insufficient_evidence` và phát `verification_completed` với `decision_code=downgraded`.

## 7. Reproducibility

- Không dùng LLM và không có yếu tố ngẫu nhiên: quyết định là tất định từ evidence + bảng luật `get_policy`. Mỗi lần chạy sinh cùng output (chỉ `event_id`/`occurred_at` trong trace khác nhau).
- Python ≥ 3.11; dependency khai báo trong `pyproject.toml` (`mcp` 2.x, `httpx2`, `jsonschema`, `python-dotenv`).
- Concurrency: các case chạy tuần tự qua `day09 run`, mỗi case một session MCP riêng; trong case, 3 specialist chạy đồng thời nhưng MCP call được tuần tự hóa bằng lock. Khoảng 0.7s/call.
- Lệnh chạy: `day09 validate-inputs`, `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip`.
- Cấu hình qua `.env` (`COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`); không ghi API key vào tài liệu hay ZIP.
- Lưu ý tương thích: `mcp` 2.x dùng tên field snake_case (`is_error`, `structured_content`, `input_schema`); `EvidenceGateway` đã được vá cho khớp.
