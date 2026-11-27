# RUNTIME-INF06 로컬 합성 비용·한도 가드 실행 근거 (`CHECKPOINT` 반영)

> [!CAUTION]
> **호스팅 실행 판정: `NO-GO` 유지 (클라우드·DB·Docker·네트워크 미실행, 제품 비용 제어 코드 아님)**
> - 본 문서는 무료 한도 초과 가능성 또는 비용·계측 불확실 시 신규 실행을 시작하지 않는 규칙(`INF-06` / `T-007F` / `OD-015`)과 회귀 보완(`RUNTIME-INF06-FIX`, `RUNTIME-INF06-CHECKPOINT`)을 **표준 라이브러리 기반 합성 계측으로만 재현·고정**한 로컬 증거입니다.
> - Notion MCP 미제공 및 외부 네트워크 금지 원칙에 따라 프롬프트 수용 기준과 [T007F_HOSTED_EXPERIMENT_PLAN.md](T007F_HOSTED_EXPERIMENT_PLAN.md) 기준으로 수행했습니다.
> - `t007a/spend_guard.py`의 기본 수치는 **테스트 전용 합성 픽스처**이며 운영 상한으로 제안하지 않습니다. 단일 프로세스 메모리 잠금(`threading.Lock`) 기반 검증은 **멀티 인스턴스 Cloud Run 조율이나 공유 GCP Billing Account / Supabase Organization의 실제 청구 보호가 검증된 것을 의미하지 않습니다.**

---

## 1. 핵심 불변식 및 정산 모델 한계

- **계측 판정 계층과 실행 시작 계층 분리**: [t007a/spend_guard.py](t007a/spend_guard.py)의 `evaluate_snapshot_for_action(...)`은 부작용 없이 유한 시각(`math.isfinite`, `>= 0`), 명시적 불리언 타입 검사(`bool()` 암묵 변환 금지), 단위 정규화(`Decimal`)를 수행한 뒤 아래 보수적 공식을 검사합니다.
  ```text
  확인된 잔여량
  - 집계 지연·공유 소비 상한
  - (진행 중 예약 + 미정산 누적 소비량)
  - 다음 요청 최대 소비량
  - 안전 정리 여유 >= 0
  ```
- **유료 기능 상태 명시 확인 강제 (`RUNTIME-INF06-CHECKPOINT`)**: `SyntheticMeterSnapshot.paid_feature_active`는 기본값 `False`를 두지 않으며 **명시적인 `False`(`type(x) is bool and x is False`)만** 유료 기능 비활성 확인으로 인정합니다. `True`는 즉시 `HALTED`(`PAID_FEATURE_DETECTED`)로 전환하고, 생략·`None`은 `COST_UNKNOWN`, `1`·`0`·문자열 등 불리언이 아닌 값은 `METERING_INVALID`로 거부하여 신규 Job·워크로드 호출을 `0`건으로 차단합니다.
- **작업 완료와 비용 정산(Settlement) 분리 및 한계**: 작업이 완료(`complete_in_flight`)되면 `in_flight_reserved`에서는 빠지지만, 해당 최대 비용과 시작된 정리 단계 비용은 `unsettled_spend`에 계속 누적되어 차감됩니다. 실제 클라우드 청구 원장과 로컬 실행 ID를 대조하는 정산 연동이 없으므로 **`observed_at_s` 타임스탬프만 갱신된 스냅샷이나 수동 재개만으로는 미정산 예산을 다시 풀지 않는 보수적 정책**을 유지합니다.
- **정리 예산 실제 확보 확인 및 시작·성공·실패 분리**: 초기 확보 정리 예산을 `0`으로 두어 유효한 스냅샷 검증을 통과한 범위 안에서만 비용 있는 정리를 허용하며, 정리 단계를 `in_progress`·`completed`·`failed`로 분리해 실패/진행 중 상태에서 완료 처리·비용 환급·자동 재시도·조기 재개를 모두 차단합니다.

---

## 2. 로컬 재현 명령 (PostgreSQL · Docker · 네트워크 불필요)

```bash
cd spikes/t007a-worker
python3 -B -m unittest discover -s tests_inf06 -p 'test_*.py' -v
```

실행 결과: 총 **`15`개 테스트 통과 (`0` 실패, `0` 건너뜀)** — 초기 가드 `7`건 + `FIX` 회귀 `6`건 + `CHECKPOINT` 입력 검증 회귀 `2`건.

---

## 3. 회귀 사례 요약표 ([tests_inf06/test_spend_guard.py](tests_inf06/test_spend_guard.py))

| 구분 | 검증 시나리오 | 수정 전 호출 수 / 현상 | 수정 후 판정 및 호출 수 | 대응 테스트 |
|---|---|---|---|---|
| **기본 7건** | 리더 분리·단위 정규화, 정상/정리 여유 침범, 누락/노후/비용불명/단위불일치 구분, `Barrier` 동시 경쟁, `100`건·`30분` 합산 상한, 중단 중 진행 예약 유지·우회 차단, 수동 재개 전제 검증 | 초기 구현 기준 통과 | 전체 유지 통과 (차단 사례 모두 신규 Job·워크로드 `0`건) | `test_pure_reader_*`, `test_case1_*` ~ `test_case6_*` |
| **FIX 1** | `17 vCPU-sec` 스냅샷에서 첫 Job 완료 후 동일/재타임스탬프 스냅샷으로 2차·3차 요청 | 2번째 Job 허용 (총 `2`건) | `unsettled_spend` 보수적 유지로 차단 (`TEARDOWN_BUFFER_INVADED`), **총 Job `1`건**(추가 `0`건) | `test_reg1_completed_job_keeps_unsettled_cost_deducted_on_same_and_re_timestamped_snapshot` |
| **FIX 2a/2b** | 정리 실행 중 예외 발생 및 `Event` 기반 정리 진행 중 중복 정리·조기 재개 시도 | 실패·진행 중에도 완료 집합에 들어가 재개 허용 | `failed`/`in_progress` 분리, 자동 재시도·중복 실행 `0`건, `TEARDOWN_INCOMPLETE`로 재개 차단(신규 Job `0`건) | `test_reg2a_*`, `test_reg2b_*` |
| **FIX 3** | 유효 예산 확인 없이 즉시 `halt()` 후 비용 있는 `DROP_SPIKE_SCHEMA` 요청 | 정리 호출 `1`건 실행 | 미확보 가드 `TEARDOWN_BUDGET_NOT_SECURED` 차단 (**정리 `0`건**), 사전 확보 가드는 `1`건 허용 | `test_reg3_immediate_halt_without_verified_budget_blocks_cost_bearing_teardown` |
| **FIX 4** | 정리 완료 이전 시각 스냅샷(`<= 60s` 이내) 및 미정산 비용 미충족 스냅샷으로 `manual_resume` | 정리 전 스냅샷으로 재개 허용 | `PRE_TEARDOWN_SNAPSHOT` 및 `HEADROOM_RECHECK_FAILED` 차단 (**신규 Job `0`건**), 정리 이후 충분한 스냅샷만 재개 | `test_reg4_manual_resume_rejects_pre_teardown_snapshot_within_freshness_and_checks_unsettled_spend` |
| **FIX 5** | `observed_at_s=NaN/±Inf/bool` 요청 및 100번째 허용 직후 가드 상태 확인 | `NaN`에서 Job `1`건 허용, 100번째 직후 `ACTIVE` 유지 | 비유한 시각 `METERING_INVALID` (**`0`건**), 100번째 허용 즉시 `HALTED` 전환(100번째 완료, 101번째 `0`건) | `test_reg5_non_finite_timestamps_blocked_and_100th_admission_immediately_halts_guard` |
| **CHECKPOINT 6a** | `SyntheticMeterSnapshot(resources=...)`에서 `paid_feature_active` 생략(및 `None`) | 생략 시 기본값 `False`로 처리되어 **신규 Job `1`건**(`workload=1`) 생성 | `COST_UNKNOWN`으로 거부, **신규 Job `0`건 / 워크로드 `0`건** | `test_reg6a_paid_feature_active_omitted_or_none_rejected_with_zero_calls` |
| **CHECKPOINT 6b** | `paid_feature_active=1`(및 `0`, `""`, `"false"`, `"true"`, `0.0`, `[]`) 전달 | `paid_feature_active=1`에서 **신규 Job `1`건**(`workload=1`) 생성 | `bool()` 암묵 변환 없이 `METERING_INVALID` 거부, **신규 Job `0`건 / 워크로드 `0`건** (`False`만 허용, `True`는 `HALTED`) | `test_reg6b_paid_feature_active_non_boolean_values_rejected_without_bool_coercion` |

---

## 4. 미실행 범위 및 한계

- **미실행 범위**: 기존 로컬 PostgreSQL · Docker `50`건 테스트 스위트, Notion 조회, 클라우드 콘솔·CLI·네트워크 실행은 본 체크포인트 범위에서 **미실행**입니다.
- **호스팅 `NO-GO` 유지**: 본 커밋은 로컬 합성 검증의 기준점일 뿐이며, 멀티 인스턴스 분산 예약이나 공유 청구 계정 보호·실제 잔여 무료 한도는 검증되지 않았으므로 호스팅은 계속 **`NO-GO`**입니다.
