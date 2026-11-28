# Track 3 Subscription LLM Hosting, Transmission & Zero-Overage Policy Audit (`T-006` · `OD-002` · `OD-014`)

- **Tasks / Decisions**: `T-006` (`Done`), `OD-002` (`Accepted`), `OD-014` (`Accepted`), `T-408` / `INF-08`
- **Redacted Policy Audit Bundle**: `contracts/llm-gate/t006_policy_audit.v1.json`
- **Hosted Runtime Enforcement**: `apps/api/src/career_memory_api/llm_gate/policy_gate.py` (`LlmRuntimeMode.OFF_MANUAL_ONLY`, all 4 `HostedLlmApprovalConfig` flags `False`, `default_adapter=None`)
- **Immutable M0 OpenAPI Pin**: `contracts/import-v1/openapi.yaml` (`07594d75425744d6149250a9f7ff95ac24659f80`)

---

## 1. Executive Summary (`ADR-020` Official Resolution)

### 한국어 요약
`T-006` 및 `ADR-020`("공식 호스팅 승인·구독 포함 한도 보장·전송 정책이 미확인되거나 유료 과금 위험이 있으면 외부 AI 호출은 `OFF` 유지 및 무료 대안 안내")에 따라 OpenAI, Anthropic, Google 3개 공급자의 제3자 호스팅 구독 OAuth 및 데이터 처리 약관을 실사했습니다.

1. **원격 호스티드(Cloud Run `cm-web` / `cm-api`) 결정 — 공식 `OFF_MANUAL_ONLY` 확정**:
   - **OpenAI (`Sign in with ChatGPT`)**: 오픈소스·로컬 호스팅(`127.0.0.1`)에서는 사용자의 ChatGPT Plus/Pro/Business 플랜 할당량을 공유하는 공식 절차(`chatgpt.tokens.use.direct`, `store: false`, `stream: true`, 무음 유료 API 폴백 없음, API 입력 학습 미사용)가 제공되나, **Cloud Run과 같은 원격 호스팅 웹 서비스는 OpenAI의 별도 파트너 심사·승인 대상**이며 코드 공개나 로컬 Codex 로그인만으로는 원격 호스팅 권한이 부여되지 않습니다.
   - **Anthropic (`Claude Pro / Max`)**: 소비자 구독 OAuth는 `Claude.ai` 및 공식 `Claude Code` 등 자사 도구로만 제한되며 제3자 웹 백엔드의 추론 대행은 약관상 금지됩니다 (`CLAUDE_SUBSCRIPTION_UNSUPPORTED`).
   - **Google (`Google AI Pro` / `Gemini API Free Tier`)**: 소비자 구독 공유 OAuth는 제공되지 않으며, 무료(Unpaid) Gemini API Free Tier는 **프롬프트·코드가 제품 개선/모델 학습 및 인적 리뷰에 사용될 수 있어** 민감도 마스킹 없이 공개 원문을 전송하는 MVP 범위(`PM-SEC-01` Post-MVP, `OD-002`, `ER-008`)와 충돌하고, 학습 제외를 위해 유료 결제(Paid Tier)를 연결하면 `ADR-020`의 무과금(`zero-overage`)·BYOK 금지 원칙과 충돌합니다.
2. **`OD-002` 및 `OD-014` 확정 내용**:
   - **`OD-002` (`Accepted`)**: OpenAI 원격 호스팅 파트너 승인이 발급되기 전까지 호스티드 환경의 외부 LLM 원문 전송은 **`OFF`**를 유지하며, 승인 전까지 사용자는 비용 `₩0`·외부 유출 `0`건이 보장되는 결정론적 수집·이벤트 큐레이션·수동 Fact/Claim 작성·방안 C 실시간 재조회 검증·무인용 Export·검색·삭제 전체 흐름을 사용합니다.
   - **`OD-014` (`Accepted`)**: 호스티드 연동 `OFF` 상태에서 PostgreSQL 사용자 LLM 토큰 테이블 `0`개, GCP Secret Manager 내 LLM 토큰 시크릿 `0`개(기존 `4/4` Free Tier 한도 보존)를 확정합니다. 향후 OpenAI 원격 호스팅 승인이 발급될 경우에만 `T-408`/`INF-08`에서 검증된 외부 암호화 Vault + 3단계 해제(`LOCAL_FENCE -> REMOTE_REVOKE -> LOCAL_SECRET_WIPE`)를 활성화합니다.
3. **3단계 릴리스 게이트 정직성 유지 (`T-506` / `ADR-018`)**:
   - 실제 외부 구독 LLM 호출이 `0`회이므로 `T-506`의 3번째 티어인 **`LIVE_SUBSCRIPTION_AI_PILOT_GATE`는 합성 테스트나 정책 감사만으로 절대 `GO`로 허위 승격하지 않고 `NO-GO (HELD_PENDING_OD002_OD014)`를 정직하게 유지**합니다 (반면 비-AI 호스티드 클로즈드 파일럿 게이트인 `PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE`는 **`GO`** 상태로 즉시 운영 가능).

---

## 2. Provider-by-Provider Audit Matrix

| Provider / Path | Remote Cloud Run Subscription OAuth | Zero-Overage Guarantee | Unmasked Public Code Privacy (`OD-002`) | Verdict |
|---|---|---|---|---|
| **OpenAI `Sign in with ChatGPT`** (`developers.openai.com/siwc/*`) | Local/OSS self-serve only; **remote hosted web requires OpenAI rollout approval** (`website`) | Yes on approved `SIWC` (`no silent paid API fallback`; `max_output_tokens` not enforced in preview) | API data not used for training; requires `store: false, stream: true` + explicit per-repo non-recallable user consent | **HOLD (`OFF_MANUAL_ONLY` on Cloud Run until OpenAI remote approval)** |
| **Anthropic Claude Pro/Max OAuth** (`code.claude.com/.../legal-and-compliance`) | **Prohibited** for third-party hosted apps (Console API keys required) | N/A (metered Console API) | N/A | **REJECTED (`CLAUDE_SUBSCRIPTION_UNSUPPORTED`)** |
| **Google Gemini Subscription / Unpaid API Free Tier** (`ai.google.dev/gemini-api/docs/pricing`) | Subscription OAuth unsupported; Unpaid API Free Tier is per-project API key | Unpaid tier is `$0`, Paid tier has metered billing risk | **Conflict**: Unpaid tier allows human review & model training; Paid tier violates no-BYOK / zero-overage rule | **REJECTED (`GEMINI_SUBSCRIPTION_UNSUPPORTED`)** |

---

## 3. Reopen Criteria (향후 라이브 AI 게이트 전환 조건)
1. Career Memory의 Cloud Run 도메인에 대해 OpenAI의 공식 원격 `Sign in with ChatGPT` 호스팅 승인 획득 (`hosted_deployment_approved=True`).
2. `OD-014` 외부 암호화 토큰 저장소 프로비저닝 및 `INF-08` 3단계 연결 해제·원격 폐기 실환경 검증 (`od014_token_store_verified=True`).
3. 격리된 클로즈드 파일럿에서 실제 구독 모델 응답에 대한 인간 감사 기반 Groundedness 및 `R-002` 리뷰 시간 평가(`T-407A` Live) 완료 후 `LIVE_SUBSCRIPTION_AI_PILOT_GATE` 재판정.
