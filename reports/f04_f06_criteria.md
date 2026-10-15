# F04–F06 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip，也没有空测试。

## F04 模型与工具准入网关

| 验收 | 测试 |
|---|---|
| 无授权工具拒绝 | `tests/integration/test_gateway.py::test_unauthorized_tool_and_egress_are_denied` |
| 无授权出口拒绝 | `tests/integration/test_gateway.py::test_unauthorized_tool_and_egress_are_denied` |
| 只读工具仍审查数据出口 | `tests/integration/test_gateway.py::test_readonly_tool_still_checks_egress` |
| 请求体不能改写 token 的 project/task | `tests/integration/test_gateway.py::test_body_cannot_override_token_project_or_task` |
| provider key 不进入返回值或审计 | `tests/integration/test_gateway.py::test_provider_key_is_redacted_from_result_and_audit` |
| LiteLLM 对 fake upstream 发真实 HTTP，响应当场采用 | `tests/integration/test_gateway.py::test_litellm_uses_real_upstream_response` |
| 上游错误不是固定成功 | `tests/integration/test_gateway.py::test_upstream_error_is_not_success` |
| 上游超时记为 unknown，费用不是 0 | `tests/integration/test_gateway.py::test_upstream_timeout_is_unknown_not_zero` |

## F05 事务预算

| 验收 | 测试 |
|---|---|
| 超出预算的预留被拒绝 | `tests/integration/test_budget.py::test_budget_overrun_is_rejected` |
| 并发 reservation 原子限额 | `tests/integration/test_budget.py::test_concurrent_reservation_is_atomic` |
| 并发 token 预留不超过 token_limit | `tests/integration/test_budget.py::test_concurrent_token_reservations_stay_within_limit` |
| 同一幂等键、相同金额、不同请求被拒绝 | `tests/integration/test_budget.py::test_same_key_different_request_is_rejected` |
| 重复结算不重复扣款 | `tests/integration/test_budget.py::test_duplicate_settlement_does_not_double_charge` |
| 实际费用大于预留时拒绝结算 | `tests/integration/test_budget.py::test_settle_rejects_actual_above_reservation` |
| 超时可能计费，预留保持 held | `tests/integration/test_budget.py::test_timeout_keeps_reservation_held` |
| unknown usage 不以 0 入账 | `tests/integration/test_budget.py::test_unknown_usage_is_not_stored_as_zero` |
| planner/repair/judge/coder 共用账本 | `tests/integration/test_budget.py::test_internal_roles_share_one_ledger` |
| verification 预留不受 planner 占用 | `tests/integration/test_budget.py::test_verification_reserve_is_protected` |

## F06 审批与对账

| 验收 | 测试 |
|---|---|
| 错误意图哈希、错误 owner、过期审批被拒绝 | `tests/integration/test_operations.py::test_approval_rejects_wrong_hash_owner_and_expiry` |
| 拒绝后的审批不能发出 | `tests/integration/test_operations.py::test_rejected_approval_cannot_dispatch` |
| 意图里的资源版本变化后不能沿用旧审批 | `tests/integration/test_operations.py::test_changed_resource_version_requires_new_approval` |
| 发出前读取资源当前版本，旧版本审批被拒绝 | `tests/integration/test_operations.py::test_dispatch_rejects_stale_live_resource_version` |
| 响应丢失后重试不重复副作用 | `tests/integration/test_operations.py::test_lost_success_response_does_not_repeat_side_effect` |
| 对账版本不一致时不确认 | `tests/integration/test_operations.py::test_reconcile_mismatch_does_not_confirm` |
| 连接重置视为 unknown，非幂等操作进入 NEEDS_REVIEW | `tests/integration/test_operations.py::test_connection_reset_is_unknown_and_needs_review` |
| httpx 连接、读超时和协议错误视为 unknown | `tests/integration/test_operations.py::test_httpx_transport_errors_stay_unknown` |
| 包装后的 httpx 传输错误仍视为 unknown | `tests/integration/test_operations.py::test_wrapped_httpx_error_stays_unknown` |
| 确认后的资源版本只经 note_resource_version 写入 | `tests/integration/test_operations.py::test_confirmed_resource_write_uses_note_resource_version` |
| 派发前对 resource_heads 行 FOR UPDATE | `tests/integration/test_operations.py::test_dispatch_locks_resource_head_before_send` |
| 非幂等 unknown 进入 NEEDS_REVIEW，且执行角色不被 API 触发器拒绝 | `tests/integration/test_operations.py::test_non_idempotent_unknown_needs_review_and_cancel_is_not_stopped` |
| 取消请求不表示已经停止 | `tests/integration/test_operations.py::test_non_idempotent_unknown_needs_review_and_cancel_is_not_stopped` |

## F03 补充

| 要求 | 测试 |
|---|---|
| 上传按块读取，超过上限即拒绝 | `tests/integration/test_artifacts.py::test_upload_stops_when_stream_exceeds_limit` |
| API 对超限上传返回 413 | `tests/integration/test_artifacts.py::test_api_rejects_streamed_oversize_upload` |
| 并发相同幂等键只产生一个产物且不返回 500 | `tests/integration/test_artifacts.py::test_concurrent_artifact_upload_returns_one_row` |
| `AETHER_BOOTSTRAP_PATH` 可用，但必须留在 `secrets/` | `tests/unit/test_path_confinement.py::test_bootstrap_file_must_stay_under_secrets` |
