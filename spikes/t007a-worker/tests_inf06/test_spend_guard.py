"""Stdlib-only unit tests for RUNTIME-INF06 synthetic spend guard.

Run from `spikes/t007a-worker/` without PostgreSQL, Docker, network, or pytest plugins:
    python3 -B -m unittest discover -s tests_inf06 -p 'test_*.py' -v
"""

from __future__ import annotations

from decimal import Decimal
import threading
import unittest

from t007a.spend_guard import (
    ActionKind,
    DecisionReason,
    Dimension,
    FakeExecutor,
    GuardState,
    ManualMonotonicClock,
    Quantity,
    ResourceMetricSnapshot,
    ResumeReason,
    SpendGuard,
    SyntheticMeterSnapshot,
    TeardownStep,
    Unit,
    build_synthetic_test_policy,
    evaluate_snapshot_for_action,
)


def _healthy_snapshot(
    observed_at_s: float = 0.0,
    *,
    paid_feature_active: bool | None = False,
    overrides: dict[str, ResourceMetricSnapshot | None] | None = None,
) -> SyntheticMeterSnapshot:
    """Synthetic test-only snapshot with mixed compatible units (GiB/MB/MiB, vcpu_sec/min)."""
    resources: dict[str, ResourceMetricSnapshot] = {
        "cloud_run_cpu": ResourceMetricSnapshot(
            verified_remaining=Quantity(20, Unit.VCPU_MIN),  # 1200 vcpu_sec
            metering_lag_and_shared_bound=Quantity(200, Unit.VCPU_SEC),
            observed_at_s=observed_at_s,
        ),
        "cloud_run_mem": ResourceMetricSnapshot(
            verified_remaining=Quantity(500, Unit.GIB_SEC),
            metering_lag_and_shared_bound=Quantity(100, Unit.GIB_SEC),
            observed_at_s=observed_at_s,
        ),
        "artifact_registry_storage": ResourceMetricSnapshot(
            verified_remaining=Quantity(400, Unit.MIB),
            metering_lag_and_shared_bound=Quantity(150, Unit.MIB),
            observed_at_s=observed_at_s,
        ),
        "cloud_logging_ingestion": ResourceMetricSnapshot(
            verified_remaining=Quantity(1, Unit.GIB),
            metering_lag_and_shared_bound=Quantity(200, Unit.MIB),
            observed_at_s=observed_at_s,
        ),
        "secret_versions": ResourceMetricSnapshot(
            verified_remaining=Quantity(3, Unit.COUNT),
            metering_lag_and_shared_bound=Quantity(1, Unit.COUNT),
            observed_at_s=observed_at_s,
        ),
        "supabase_uncached_egress": ResourceMetricSnapshot(
            # Mixed decimal GB + binary MiB normalized to exact canonical bytes
            verified_remaining=Quantity(2, Unit.GB),
            metering_lag_and_shared_bound=Quantity(500, Unit.MB),
            observed_at_s=observed_at_s,
        ),
    }
    if overrides:
        for k, v in overrides.items():
            if v is None:
                resources.pop(k, None)
            else:
                resources[k] = v
    return SyntheticMeterSnapshot(resources=resources, paid_feature_active=paid_feature_active)


class SpendGuardInf06Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualMonotonicClock(1_000.0)
        self.policy = build_synthetic_test_policy()
        self.executor = FakeExecutor(self.clock)
        self.guard = SpendGuard(policy=self.policy, clock=self.clock, executor=self.executor)

    def test_pure_reader_is_separated_from_executor_and_normalizes_units(self) -> None:
        snap = _healthy_snapshot(observed_at_s=self.clock.now())
        eval_res = evaluate_snapshot_for_action(
            snapshot=snap,
            policy=self.policy,
            action=ActionKind.JOB_CREATE,
            now_s=self.clock.now(),
            in_flight_reserved={},
        )
        self.assertTrue(eval_res.ok)
        self.assertEqual(eval_res.reason, DecisionReason.ALLOWED)
        # Pure evaluation never triggers FakeExecutor
        self.assertEqual(self.executor.workload_calls_total, 0)
        self.assertEqual(self.executor.teardown_calls_total, 0)

        # Verify mixed GB/MB/KiB/MiB arithmetic in canonical bytes on supabase_uncached_egress:
        # 2 GB (2_000_000_000 B) - 500 MB (500_000_000 B) - 0 - 128 KiB (131_072 B) - 50 MiB (52_428_800 B)
        egress = eval_res.breakdowns["supabase_uncached_egress"]
        self.assertEqual(egress.dimension, Dimension.BYTES)
        expected_after = (
            Decimal(2_000_000_000)
            - Decimal(500_000_000)
            - Decimal(0)
            - Decimal(128 * 1_024)
            - Decimal(50 * 1_024 * 1_024)
        )
        self.assertEqual(egress.headroom_after_all, expected_after)

    def test_case1_normal_executes_once_and_teardown_buffer_invasion_executes_zero(self) -> None:
        # 1a. Sufficient headroom across all resources -> executes exactly once.
        snap_ok = _healthy_snapshot(observed_at_s=self.clock.now())
        d_ok = self.guard.execute(ActionKind.JOB_CREATE, snap_ok)
        self.assertTrue(d_ok.allowed)
        self.assertEqual(d_ok.reason, DecisionReason.ALLOWED)
        self.assertEqual(self.executor.jobs_created, 1)
        self.assertEqual(self.executor.workload_calls_total, 1)

        # 1b. Request would leave positive raw remaining BEFORE teardown buffer (+30 MiB),
        #     but invades the 50 MiB safe_teardown_buffer (-20 MiB after buffer) -> 0 new calls.
        executor_blocked = FakeExecutor(self.clock)
        guard_blocked = SpendGuard(policy=self.policy, clock=self.clock, executor=executor_blocked)
        snap_invades_buffer = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={
                "supabase_uncached_egress": ResourceMetricSnapshot(
                    # 130 MiB - 100 MiB lag = 30 MiB (> 128 KiB request cost, but < 50 MiB teardown buffer)
                    verified_remaining=Quantity(130, Unit.MIB),
                    metering_lag_and_shared_bound=Quantity(100, Unit.MIB),
                    observed_at_s=self.clock.now(),
                )
            },
        )
        d_buf = guard_blocked.execute(ActionKind.JOB_CREATE, snap_invades_buffer)
        self.assertFalse(d_buf.allowed)
        self.assertEqual(d_buf.reason, DecisionReason.TEARDOWN_BUFFER_INVADED)
        self.assertGreater(
            d_buf.evaluation.breakdowns["supabase_uncached_egress"].available_before_teardown_buffer,
            Decimal(0),
        )
        self.assertLess(
            d_buf.evaluation.breakdowns["supabase_uncached_egress"].headroom_after_all,
            Decimal(0),
        )
        self.assertEqual(executor_blocked.jobs_created, 0)
        self.assertEqual(executor_blocked.workload_calls_total, 0)

        # 1c. Direct quota overrun even before teardown buffer -> QUOTA_HEADROOM_EXCEEDED & 0 calls.
        snap_overrun = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={
                "cloud_run_cpu": ResourceMetricSnapshot(
                    verified_remaining=Quantity(10, Unit.VCPU_SEC),
                    metering_lag_and_shared_bound=Quantity(9, Unit.VCPU_SEC),  # only 1s left < 4s wake
                    observed_at_s=self.clock.now(),
                )
            },
        )
        d_over = guard_blocked.execute(ActionKind.WAKE, snap_overrun)
        self.assertFalse(d_over.allowed)
        self.assertEqual(d_over.reason, DecisionReason.QUOTA_HEADROOM_EXCEEDED)
        self.assertEqual(executor_blocked.workload_calls_total, 0)

    def test_case2_missing_stale_unknown_and_unit_mismatch_distinguished_with_no_fallback(self) -> None:
        # 2a. Missing resource metric -> METERING_MISSING
        snap_missing = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={"artifact_registry_storage": None},
        )
        d_missing = self.guard.execute(ActionKind.JOB_CREATE, snap_missing)
        self.assertFalse(d_missing.allowed)
        self.assertEqual(d_missing.reason, DecisionReason.METERING_MISSING)

        # 2b. Stale metric (age 61s > max_staleness_s 60s) -> METERING_STALE
        snap_stale = _healthy_snapshot(observed_at_s=self.clock.now() - 61.0)
        d_stale = self.guard.execute(ActionKind.JOB_CREATE, snap_stale)
        self.assertFalse(d_stale.allowed)
        self.assertEqual(d_stale.reason, DecisionReason.METERING_STALE)

        # 2c. Unknown cost bound (unestimated action AND unknown lag bound) -> COST_UNKNOWN
        snap_fresh = _healthy_snapshot(observed_at_s=self.clock.now())
        d_unknown_action = self.guard.execute(ActionKind.UNESTIMATED_OP, snap_fresh)
        self.assertFalse(d_unknown_action.allowed)
        self.assertEqual(d_unknown_action.reason, DecisionReason.COST_UNKNOWN)

        snap_unknown_lag = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={
                "cloud_run_cpu": ResourceMetricSnapshot(
                    verified_remaining=Quantity(500, Unit.VCPU_SEC),
                    metering_lag_and_shared_bound=None,
                    observed_at_s=self.clock.now(),
                )
            },
        )
        d_unknown_lag = self.guard.execute(ActionKind.JOB_CREATE, snap_unknown_lag)
        self.assertFalse(d_unknown_lag.allowed)
        self.assertEqual(d_unknown_lag.reason, DecisionReason.COST_UNKNOWN)

        # 2d. Incompatible unit dimension (bytes supplied for CPU time) -> UNIT_MISMATCH
        snap_bad_unit = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={
                "cloud_run_cpu": ResourceMetricSnapshot(
                    verified_remaining=Quantity(500, Unit.MIB),
                    metering_lag_and_shared_bound=Quantity(10, Unit.VCPU_SEC),
                    observed_at_s=self.clock.now(),
                )
            },
        )
        d_unit = self.guard.execute(ActionKind.JOB_CREATE, snap_bad_unit)
        self.assertFalse(d_unit.allowed)
        self.assertEqual(d_unit.reason, DecisionReason.UNIT_MISMATCH)

        # Distinct reason codes & strictly zero workload or fallback executions across all 5 blocks.
        self.assertEqual(
            len({d_missing.reason, d_stale.reason, d_unknown_action.reason, d_unit.reason}),
            4,
        )
        self.assertEqual(self.executor.workload_calls_total, 0)
        self.assertEqual(self.executor.fallback_calls, 0)
        self.assertEqual(self.executor.teardown_calls_total, 0)

    def test_case3_concurrent_contention_single_slot_barrier_without_sleep(self) -> None:
        # Configure CPU headroom for AT MOST ONE JOB_CREATE (cost = 2 vcpu_sec, buffer = 5 vcpu_sec):
        # verified_remaining (17) - lag (10) - buffer (5) = 2 vcpu_sec -> exact room for 1 job!
        snap_one_slot = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={
                "cloud_run_cpu": ResourceMetricSnapshot(
                    verified_remaining=Quantity(17, Unit.VCPU_SEC),
                    metering_lag_and_shared_bound=Quantity(10, Unit.VCPU_SEC),
                    observed_at_s=self.clock.now(),
                )
            },
        )

        winner_entered = threading.Event()
        release_winner = threading.Event()
        start_barrier = threading.Barrier(2)

        def _hold_in_executor(_action: ActionKind, _lease_id: str) -> None:
            winner_entered.set()
            release_winner.wait(timeout=5.0)

        contended_executor = FakeExecutor(self.clock, on_workload_enter=_hold_in_executor)
        contended_guard = SpendGuard(
            policy=self.policy,
            clock=self.clock,
            executor=contended_executor,
        )

        decisions: list = [None, None]

        def _worker(idx: int) -> None:
            start_barrier.wait(timeout=5.0)
            decisions[idx] = contended_guard.execute(
                ActionKind.JOB_CREATE,
                snap_one_slot,
                detail=f"contender-{idx}",
            )
            # As soon as the rejected contender gets its synchronous rejection, release the winner.
            if not decisions[idx].allowed:
                winner_entered.wait(timeout=5.0)
                release_winner.set()

        t0 = threading.Thread(target=_worker, args=(0,))
        t1 = threading.Thread(target=_worker, args=(1,))
        t0.start()
        t1.start()
        t0.join(timeout=5.0)
        t1.join(timeout=5.0)

        allowed = [d for d in decisions if d is not None and d.allowed]
        blocked = [d for d in decisions if d is not None and not d.allowed]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(len(blocked), 1)
        self.assertIn(
            blocked[0].reason,
            {DecisionReason.QUOTA_HEADROOM_EXCEEDED, DecisionReason.TEARDOWN_BUFFER_INVADED},
        )
        self.assertEqual(contended_guard.peak_in_flight_count, 1)
        self.assertEqual(contended_executor.jobs_created, 1)
        self.assertEqual(contended_executor.workload_calls_total, 1)

    def test_case4_unified_100_request_cap_and_30_minute_window(self) -> None:
        # 4a. External call, job_create, wake, retry, recover, probe share ONE counter up to 100.
        snap = _healthy_snapshot(observed_at_s=self.clock.now())
        rotation = (
            ActionKind.EXTERNAL_CALL,
            ActionKind.JOB_CREATE,
            ActionKind.WAKE,
            ActionKind.RETRY,
            ActionKind.RECOVER,
            ActionKind.PROBE,
        )
        for i in range(100):
            d = self.guard.execute(rotation[i % len(rotation)], snap)
            self.assertTrue(d.allowed, f"request #{i + 1} should succeed")

        self.assertEqual(self.guard.shared_request_count, 100)
        self.assertEqual(self.executor.workload_calls_total, 100)
        self.assertGreater(self.executor.external_calls, 0)
        self.assertGreater(self.executor.wakes, 0)
        self.assertGreater(self.executor.retries, 0)
        self.assertGreater(self.executor.recovers, 0)
        self.assertGreater(self.executor.probes, 0)

        # Request #101 (any workload kind) is blocked and latches HALTED -> 0 additional calls.
        d_101 = self.guard.execute(ActionKind.PROBE, snap)
        self.assertFalse(d_101.allowed)
        self.assertEqual(d_101.reason, DecisionReason.REQUEST_CAP_EXCEEDED)
        self.assertEqual(self.guard.state, GuardState.HALTED)
        self.assertEqual(self.executor.workload_calls_total, 100)

        # 4b. Separate guard instance testing the 30-minute (1800s) elapsed window cutoff.
        clock_w = ManualMonotonicClock(0.0)
        exec_w = FakeExecutor(clock_w)
        guard_w = SpendGuard(policy=self.policy, clock=clock_w, executor=exec_w)

        clock_w.set(1_799.0)  # 29m59s -> still inside 30-minute window
        d_before = guard_w.execute(ActionKind.EXTERNAL_CALL, _healthy_snapshot(observed_at_s=1_799.0))
        self.assertTrue(d_before.allowed)
        self.assertEqual(exec_w.workload_calls_total, 1)

        clock_w.set(1_800.0)  # 30m00s -> blocked and latched HALTED
        d_at_30m = guard_w.execute(ActionKind.JOB_CREATE, _healthy_snapshot(observed_at_s=1_800.0))
        self.assertFalse(d_at_30m.allowed)
        self.assertEqual(d_at_30m.reason, DecisionReason.WINDOW_ELAPSED)
        self.assertEqual(guard_w.state, GuardState.HALTED)

        clock_w.set(1_805.0)  # after 30m -> remains blocked
        d_after_30m = guard_w.execute(ActionKind.RECOVER, _healthy_snapshot(observed_at_s=1_805.0))
        self.assertFalse(d_after_30m.allowed)
        self.assertEqual(d_after_30m.reason, DecisionReason.GUARD_HALTED)
        self.assertEqual(exec_w.workload_calls_total, 1)  # 0 new calls at/after 30m

    def test_case5_halt_keeps_in_flight_reservation_blocks_workloads_and_rejects_teardown_bypass(self) -> None:
        snap = _healthy_snapshot(observed_at_s=self.clock.now())

        # Start 1 in-flight job BEFORE halt (2 vcpu_sec = 2000 vcpu_ms reserved).
        d_started, handle = self.guard.begin_in_flight(ActionKind.JOB_CREATE, snap, detail="pre-halt-job")
        self.assertTrue(d_started.allowed)
        self.assertIsNotNone(handle)
        baseline_workload_calls = self.executor.workload_calls_total
        self.assertEqual(baseline_workload_calls, 1)

        # Halt the guard while `handle` is still running.
        self.guard.halt("BUDGET_SPIKE_DETECTED")
        self.assertEqual(self.guard.state, GuardState.HALTED)

        # In-flight reservation is NOT assumed auto-cancelled!
        reserved_during_halt = self.guard.in_flight_reserved_snapshot()
        self.assertEqual(reserved_during_halt["cloud_run_cpu"], Decimal(2_000))
        eval_while_in_flight = evaluate_snapshot_for_action(
            snapshot=snap,
            policy=self.policy,
            action=ActionKind.PROBE,
            now_s=self.clock.now(),
            in_flight_reserved=reserved_during_halt,
        )
        self.assertEqual(
            eval_while_in_flight.breakdowns["cloud_run_cpu"].in_flight_reserved,
            Decimal(2_000),
        )

        # Post-halt new Job, Retry, Wake, Recover -> ALL 0 executions.
        for blocked_kind in (
            ActionKind.JOB_CREATE,
            ActionKind.RETRY,
            ActionKind.WAKE,
            ActionKind.RECOVER,
        ):
            res = self.guard.execute(blocked_kind, snap)
            self.assertFalse(res.allowed)
            self.assertEqual(res.reason, DecisionReason.GUARD_HALTED)
        self.assertEqual(self.executor.workload_calls_total - baseline_workload_calls, 0)

        # Normal work attempting to bypass halt through `execute_teardown` MUST fail with 0 calls.
        bypass_1 = self.guard.execute_teardown(ActionKind.JOB_CREATE)
        bypass_2 = self.guard.execute_teardown("delete_cloud_run_services")  # raw string rejected
        bypass_3 = self.guard.execute_teardown(
            TeardownStep.STOP_CALLER,
            requested_action=ActionKind.RETRY,
        )
        for bp in (bypass_1, bypass_2, bypass_3):
            self.assertFalse(bp.allowed)
            self.assertEqual(bp.reason, DecisionReason.TEARDOWN_BYPASS_REJECTED)
        self.assertEqual(self.executor.workload_calls_total - baseline_workload_calls, 0)
        self.assertEqual(self.executor.teardown_calls_total, 0)

        # Pre-secured safe teardown steps ARE allowed while in-flight reservation is still tracked.
        td_res = self.guard.execute_teardown(TeardownStep.STOP_CALLER)
        self.assertTrue(td_res.allowed)
        self.assertEqual(td_res.reason, DecisionReason.TEARDOWN_ALLOWED)
        self.assertEqual(td_res.in_flight_reserved["cloud_run_cpu"], Decimal(2_000))
        self.assertEqual(self.executor.teardown_calls_total, 1)
        self.assertEqual(self.executor.workload_calls_total - baseline_workload_calls, 0)

        # Only when the in-flight job explicitly finishes does its reservation drop to 0.
        handle.finish()
        self.assertEqual(self.guard.in_flight_reserved_snapshot()["cloud_run_cpu"], Decimal(0))

    def test_case6_manual_resume_never_auto_resumes_and_verifies_all_prerequisites(self) -> None:
        snap_t0 = _healthy_snapshot(observed_at_s=self.clock.now())
        _, in_flight = self.guard.begin_in_flight(ActionKind.EXTERNAL_CALL, snap_t0)
        self.assertEqual(self.executor.workload_calls_total, 1)

        # Trigger halt via paid-feature kill-switch detection.
        snap_paid = _healthy_snapshot(observed_at_s=self.clock.now(), paid_feature_active=True)
        d_halt = self.guard.execute(ActionKind.JOB_CREATE, snap_paid)
        self.assertFalse(d_halt.allowed)
        self.assertEqual(d_halt.reason, DecisionReason.PAID_FEATURE_DETECTED)
        self.assertEqual(self.guard.state, GuardState.HALTED)
        self.assertEqual(self.executor.workload_calls_total, 1)

        # 6a. Fresh healthy snapshot arrives -> metering refresh alone MUST NOT auto-resume!
        self.clock.advance(10.0)
        snap_fresh = _healthy_snapshot(observed_at_s=self.clock.now(), paid_feature_active=False)
        d_still_halted = self.guard.execute(ActionKind.JOB_CREATE, snap_fresh)
        self.assertFalse(d_still_halted.allowed)
        self.assertEqual(d_still_halted.reason, DecisionReason.GUARD_HALTED)
        self.assertEqual(self.executor.jobs_created, 0)

        # 6b. Resume rejected when cause_resolved=False.
        r_cause = self.guard.manual_resume(
            snapshot=snap_fresh,
            cause_resolved=False,
            operator_approval="owner-approved-01",
        )
        self.assertFalse(r_cause.resumed)
        self.assertEqual(r_cause.reason, ResumeReason.CAUSE_NOT_RESOLVED)

        # 6c. Resume rejected when required safe teardown steps are incomplete.
        r_td = self.guard.manual_resume(
            snapshot=snap_fresh,
            cause_resolved=True,
            operator_approval="owner-approved-01",
        )
        self.assertFalse(r_td.resumed)
        self.assertEqual(r_td.reason, ResumeReason.TEARDOWN_INCOMPLETE)

        # Complete all pre-secured safe teardown steps.
        for step in TeardownStep:
            td = self.guard.execute_teardown(step)
            self.assertTrue(td.allowed)
        self.assertEqual(self.executor.teardown_calls_total, len(TeardownStep))

        # 6d. Resume rejected while pre-halt in-flight work is still active.
        r_inflight = self.guard.manual_resume(
            snapshot=snap_fresh,
            cause_resolved=True,
            operator_approval="owner-approved-01",
        )
        self.assertFalse(r_inflight.resumed)
        self.assertEqual(r_inflight.reason, ResumeReason.IN_FLIGHT_WORK_ACTIVE)

        in_flight.finish()

        # 6e. Resume rejected if post-teardown snapshot is stale or lacks headroom.
        self.clock.advance(120.0)  # snap_fresh is now 120s old (> 60s max_staleness_s)
        r_stale = self.guard.manual_resume(
            snapshot=snap_fresh,
            cause_resolved=True,
            operator_approval="owner-approved-01",
        )
        self.assertFalse(r_stale.resumed)
        self.assertEqual(r_stale.reason, ResumeReason.HEADROOM_RECHECK_FAILED)

        # 6f. Resume rejected if explicit operator approval string is blank.
        snap_rechecked = _healthy_snapshot(observed_at_s=self.clock.now(), paid_feature_active=False)
        r_no_approval = self.guard.manual_resume(
            snapshot=snap_rechecked,
            cause_resolved=True,
            operator_approval="   ",
        )
        self.assertFalse(r_no_approval.resumed)
        self.assertEqual(r_no_approval.reason, ResumeReason.MISSING_OPERATOR_APPROVAL)
        self.assertEqual(self.executor.jobs_created, 0)

        # 6g. All conditions satisfied + explicit manual_resume -> resumes cleanly and allows 1 new job.
        r_ok = self.guard.manual_resume(
            snapshot=snap_rechecked,
            cause_resolved=True,
            operator_approval="owner-approved-01",
        )
        self.assertTrue(r_ok.resumed)
        self.assertEqual(r_ok.reason, ResumeReason.RESUMED)
        self.assertEqual(self.guard.state, GuardState.ACTIVE)
        self.assertEqual(self.guard.shared_request_count, 0)

        d_after_resume = self.guard.execute(ActionKind.JOB_CREATE, snap_rechecked)
        self.assertTrue(d_after_resume.allowed)
        self.assertEqual(self.executor.jobs_created, 1)

    def test_reg1_completed_job_keeps_unsettled_cost_deducted_on_same_and_re_timestamped_snapshot(self) -> None:
        # CPU remaining=17 vCPU-sec, lag/shared=10, teardown buffer=5, Job max cost=2 -> room for 1 job only.
        snap_t0 = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={
                "cloud_run_cpu": ResourceMetricSnapshot(
                    verified_remaining=Quantity(17, Unit.VCPU_SEC),
                    metering_lag_and_shared_bound=Quantity(10, Unit.VCPU_SEC),
                    observed_at_s=self.clock.now(),
                )
            },
        )
        d1 = self.guard.execute(ActionKind.JOB_CREATE, snap_t0)
        self.assertTrue(d1.allowed)
        self.assertEqual(self.executor.jobs_created, 1)
        # Execution finished (in_flight == 0), so completion is distinct from settlement.
        self.assertEqual(self.guard.in_flight_reserved_snapshot()["cloud_run_cpu"], Decimal(0))

        # 2nd job with the same snapshot MUST be blocked because 2 vCPU-sec is still unsettled.
        d2 = self.guard.execute(ActionKind.JOB_CREATE, snap_t0)
        self.assertFalse(d2.allowed)
        self.assertEqual(self.executor.jobs_created, 1)
        self.assertEqual(self.executor.workload_calls_total, 1)

        # Merely refreshing observed_at_s without reconciliation proof MUST NOT release budget either.
        self.clock.advance(5.0)
        snap_re_timestamped = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={
                "cloud_run_cpu": ResourceMetricSnapshot(
                    verified_remaining=Quantity(17, Unit.VCPU_SEC),
                    metering_lag_and_shared_bound=Quantity(10, Unit.VCPU_SEC),
                    observed_at_s=self.clock.now(),
                )
            },
        )
        d3 = self.guard.execute(ActionKind.JOB_CREATE, snap_re_timestamped)
        self.assertFalse(d3.allowed)
        self.assertEqual(self.executor.jobs_created, 1)
        self.assertEqual(self.executor.workload_calls_total, 1)

    def test_reg2a_failed_teardown_step_not_marked_completed_keeps_cost_and_blocks_resume(self) -> None:
        class FailingSchemaDropExecutor(FakeExecutor):
            def run_teardown(self, step: TeardownStep, lease_id: str, detail: str = ""):
                rec = super().run_teardown(step, lease_id, detail=detail)
                if step == TeardownStep.DROP_SPIKE_SCHEMA:
                    raise RuntimeError("synthetic network error during DROP SCHEMA")
                return rec

        fail_exec = FailingSchemaDropExecutor(self.clock)
        guard = SpendGuard(policy=self.policy, clock=self.clock, executor=fail_exec)

        # Verify & secure teardown budget via 1 valid probe, then halt.
        snap_t0 = _healthy_snapshot(observed_at_s=self.clock.now())
        self.assertTrue(guard.execute(ActionKind.PROBE, snap_t0).allowed)
        guard.halt("MANUAL_STOP")

        for step in (
            TeardownStep.STOP_CALLER,
            TeardownStep.DELETE_CLOUD_RUN_SERVICES,
            TeardownStep.DELETE_ARTIFACT_IMAGES,
            TeardownStep.DESTROY_SECRETS_AND_SA,
        ):
            self.assertTrue(guard.execute_teardown(step).allowed)

        # Failing teardown step: must NOT enter completed_teardown_steps, must NOT auto-retry,
        # and must NOT assume zero cost.
        raised = False
        td_fail = None
        try:
            td_fail = guard.execute_teardown(TeardownStep.DROP_SPIKE_SCHEMA)
        except RuntimeError:
            raised = True
        if not raised:
            self.assertIsNotNone(td_fail)
            self.assertFalse(td_fail.allowed)

        self.assertNotIn(TeardownStep.DROP_SPIKE_SCHEMA, guard.completed_teardown_steps)
        self.assertEqual(fail_exec.teardown_calls_total, 5)  # 4 succeeded + 1 failed attempt (no auto-retry)

        # Subsequent call to the failed step must NOT silently re-run it.
        td_retry = guard.execute_teardown(TeardownStep.DROP_SPIKE_SCHEMA)
        self.assertFalse(td_retry.allowed)
        self.assertEqual(fail_exec.teardown_calls_total, 5)

        # Manual resume MUST be blocked even with a fresh post-teardown snapshot.
        self.clock.advance(10.0)
        snap_after = _healthy_snapshot(observed_at_s=self.clock.now())
        res = guard.manual_resume(
            snapshot=snap_after,
            cause_resolved=True,
            operator_approval="owner-approved-02",
        )
        self.assertFalse(res.resumed)
        self.assertEqual(res.reason, ResumeReason.TEARDOWN_INCOMPLETE)
        self.assertFalse(guard.execute(ActionKind.JOB_CREATE, snap_after).allowed)
        self.assertEqual(fail_exec.jobs_created, 0)

    def test_reg2b_in_progress_teardown_blocks_duplicate_step_and_premature_resume_without_sleep(self) -> None:
        td_entered = threading.Event()
        td_release = threading.Event()

        class BlockingTeardownExecutor(FakeExecutor):
            def run_teardown(self, step: TeardownStep, lease_id: str, detail: str = ""):
                rec = super().run_teardown(step, lease_id, detail=detail)
                if step == TeardownStep.DROP_SPIKE_SCHEMA:
                    td_entered.set()
                    td_release.wait(timeout=5.0)
                return rec

        block_exec = BlockingTeardownExecutor(self.clock)
        guard = SpendGuard(policy=self.policy, clock=self.clock, executor=block_exec)

        snap_t0 = _healthy_snapshot(observed_at_s=self.clock.now())
        self.assertTrue(guard.execute(ActionKind.PROBE, snap_t0).allowed)
        guard.halt("MANUAL_STOP")

        for step in (
            TeardownStep.STOP_CALLER,
            TeardownStep.DELETE_CLOUD_RUN_SERVICES,
            TeardownStep.DELETE_ARTIFACT_IMAGES,
            TeardownStep.DESTROY_SECRETS_AND_SA,
        ):
            self.assertTrue(guard.execute_teardown(step).allowed)

        worker_result: list = [None]

        def _run_slow_drop() -> None:
            worker_result[0] = guard.execute_teardown(TeardownStep.DROP_SPIKE_SCHEMA)

        t = threading.Thread(target=_run_slow_drop)
        t.start()
        self.assertTrue(td_entered.wait(timeout=5.0))

        try:
            # While DROP_SPIKE_SCHEMA is still running: not completed, duplicate blocked, resume blocked.
            self.assertNotIn(TeardownStep.DROP_SPIKE_SCHEMA, guard.completed_teardown_steps)

            dup = guard.execute_teardown(TeardownStep.DROP_SPIKE_SCHEMA)
            self.assertFalse(dup.allowed)
            self.assertEqual(block_exec.teardown_calls_total, 5)  # no duplicate execution

            self.clock.advance(5.0)
            snap_mid = _healthy_snapshot(observed_at_s=self.clock.now())
            early_resume = guard.manual_resume(
                snapshot=snap_mid,
                cause_resolved=True,
                operator_approval="owner-approved-03",
            )
            self.assertFalse(early_resume.resumed)
            self.assertEqual(early_resume.reason, ResumeReason.TEARDOWN_INCOMPLETE)
            self.assertEqual(block_exec.jobs_created, 0)
        finally:
            td_release.set()
            t.join(timeout=5.0)

        self.assertTrue(worker_result[0].allowed)
        self.assertIn(TeardownStep.DROP_SPIKE_SCHEMA, guard.completed_teardown_steps)

    def test_reg3_immediate_halt_without_verified_budget_blocks_cost_bearing_teardown(self) -> None:
        # Guard 1: halted immediately WITHOUT any valid metering check -> cost-bearing teardown = 0 calls.
        self.guard.halt("IMMEDIATE_HALT_UNVERIFIED_BUDGET")
        d_unsecured = self.guard.execute_teardown(TeardownStep.DROP_SPIKE_SCHEMA)
        self.assertFalse(d_unsecured.allowed)
        self.assertEqual(self.executor.teardown_calls_total, 0)

        # Guard 2: verified & secured teardown buffer on a valid snapshot before halt -> allowed during halt.
        exec_secured = FakeExecutor(self.clock)
        guard_secured = SpendGuard(policy=self.policy, clock=self.clock, executor=exec_secured)
        snap_ok = _healthy_snapshot(observed_at_s=self.clock.now())
        self.assertTrue(guard_secured.execute(ActionKind.PROBE, snap_ok).allowed)
        guard_secured.halt("HALT_AFTER_SECURED_BUDGET")
        d_secured = guard_secured.execute_teardown(TeardownStep.DROP_SPIKE_SCHEMA)
        self.assertTrue(d_secured.allowed)
        self.assertEqual(exec_secured.teardown_calls_total, 1)

    def test_reg4_manual_resume_rejects_pre_teardown_snapshot_within_freshness_and_checks_unsettled_spend(self) -> None:
        # t = 1000: 1 JOB_CREATE runs (commits 2 vCPU-sec).
        snap_t0 = _healthy_snapshot(observed_at_s=self.clock.now())
        self.assertTrue(self.guard.execute(ActionKind.JOB_CREATE, snap_t0).allowed)
        self.assertEqual(self.executor.jobs_created, 1)

        # t = 1005: guard halts; pre-teardown snapshot is observed at t = 1006.
        self.clock.advance(5.0)
        self.guard.halt("MANUAL_CHECK")
        self.clock.advance(1.0)
        snap_pre_teardown = _healthy_snapshot(observed_at_s=self.clock.now())  # observed_at = 1006

        # t = 1010: teardown steps complete (DELETE_CLOUD_RUN_SERVICES commits +1 vCPU-sec -> 3 vCPU-sec unsettled).
        self.clock.advance(4.0)
        for step in TeardownStep:
            self.assertTrue(self.guard.execute_teardown(step).allowed)

        # t = 1015: snap_pre_teardown is only 9s old (<= 60s max_staleness_s), MUST still be rejected!
        self.clock.advance(5.0)
        r_pre_td = self.guard.manual_resume(
            snapshot=snap_pre_teardown,
            cause_resolved=True,
            operator_approval="owner-approved-04",
        )
        self.assertFalse(r_pre_td.resumed)
        self.assertFalse(self.guard.execute(ActionKind.JOB_CREATE, snap_pre_teardown).allowed)
        self.assertEqual(self.executor.jobs_created, 1)

        # t = 1020 (> 1010 post-teardown): snapshot that ignores the 3 vCPU-sec unsettled spend MUST fail resume:
        # remaining 18 - lag 10 - unsettled 3 - probe 1 - buffer 5 = -1 < 0.
        self.clock.advance(5.0)
        snap_tight_post_td = _healthy_snapshot(
            observed_at_s=self.clock.now(),
            overrides={
                "cloud_run_cpu": ResourceMetricSnapshot(
                    verified_remaining=Quantity(18, Unit.VCPU_SEC),
                    metering_lag_and_shared_bound=Quantity(10, Unit.VCPU_SEC),
                    observed_at_s=self.clock.now(),
                )
            },
        )
        r_tight = self.guard.manual_resume(
            snapshot=snap_tight_post_td,
            cause_resolved=True,
            operator_approval="owner-approved-04",
        )
        self.assertFalse(r_tight.resumed)
        self.assertEqual(r_tight.reason, ResumeReason.HEADROOM_RECHECK_FAILED)
        self.assertEqual(self.executor.jobs_created, 1)

        # Post-teardown snapshot with sufficient headroom including unsettled spend -> resumes cleanly.
        snap_valid_post_td = _healthy_snapshot(observed_at_s=self.clock.now())
        r_ok = self.guard.manual_resume(
            snapshot=snap_valid_post_td,
            cause_resolved=True,
            operator_approval="owner-approved-04",
        )
        self.assertTrue(r_ok.resumed)
        self.assertTrue(self.guard.execute(ActionKind.JOB_CREATE, snap_valid_post_td).allowed)
        self.assertEqual(self.executor.jobs_created, 2)

    def test_reg5_non_finite_timestamps_blocked_and_100th_admission_immediately_halts_guard(self) -> None:
        # 5a. NaN / +Inf / -Inf / bool observed_at_s MUST all fail closed with 0 executions.
        for bad_ts in (float("nan"), float("inf"), float("-inf"), True):
            snap_bad = _healthy_snapshot(observed_at_s=bad_ts)  # type: ignore[arg-type]
            d_bad = self.guard.execute(ActionKind.JOB_CREATE, snap_bad)
            self.assertFalse(d_bad.allowed, f"bad_ts={bad_ts!r} must not be allowed")
        self.assertEqual(self.executor.workload_calls_total, 0)

        # 5b. 100th admitted request can proceed, but immediately transitions guard to non-accepting (HALTED).
        snap_ok = _healthy_snapshot(observed_at_s=self.clock.now())
        for _ in range(99):
            self.assertTrue(self.guard.execute(ActionKind.PROBE, snap_ok).allowed)
        self.assertEqual(self.guard.state, GuardState.ACTIVE)

        d_100, handle_100 = self.guard.begin_in_flight(ActionKind.JOB_CREATE, snap_ok)
        self.assertTrue(d_100.allowed)
        self.assertIsNotNone(handle_100)
        # Immediately after the 100th admission (while #100 is still in flight, before #101 arrives):
        self.assertEqual(self.guard.state, GuardState.HALTED)
        self.assertEqual(d_100.guard_state, GuardState.HALTED)

        handle_100.finish()
        self.assertEqual(self.executor.workload_calls_total, 100)
        self.assertEqual(self.guard.state, GuardState.HALTED)

        d_101 = self.guard.execute(ActionKind.PROBE, snap_ok)
        self.assertFalse(d_101.allowed)
        self.assertEqual(self.executor.workload_calls_total, 100)

    def test_reg6a_paid_feature_active_omitted_or_none_rejected_with_zero_calls(self) -> None:
        base_resources = _healthy_snapshot(observed_at_s=self.clock.now()).resources

        # Omitting paid_feature_active MUST NOT default to False or create any job.
        snap_omitted = SyntheticMeterSnapshot(resources=base_resources)
        d_omitted = self.guard.execute(ActionKind.JOB_CREATE, snap_omitted)
        self.assertEqual(
            (d_omitted.allowed, self.executor.jobs_created, self.executor.workload_calls_total),
            (False, 0, 0),
        )
        self.assertIn(
            d_omitted.reason,
            {DecisionReason.COST_UNKNOWN, DecisionReason.METERING_INVALID},
        )

        # Explicit None MUST also fail closed with 0 calls.
        snap_none = SyntheticMeterSnapshot(resources=base_resources, paid_feature_active=None)
        d_none = self.guard.execute(ActionKind.JOB_CREATE, snap_none)
        self.assertFalse(d_none.allowed)
        self.assertIn(
            d_none.reason,
            {DecisionReason.COST_UNKNOWN, DecisionReason.METERING_INVALID},
        )
        self.assertEqual(self.executor.jobs_created, 0)
        self.assertEqual(self.executor.workload_calls_total, 0)

    def test_reg6b_paid_feature_active_non_boolean_values_rejected_without_bool_coercion(self) -> None:
        base_resources = _healthy_snapshot(observed_at_s=self.clock.now()).resources

        # paid_feature_active=1 previously bypassed `is None` and `is True` and created 1 job.
        snap_int_one = SyntheticMeterSnapshot(
            resources=base_resources,
            paid_feature_active=1,  # type: ignore[arg-type]
        )
        d_one = self.guard.execute(ActionKind.JOB_CREATE, snap_int_one)
        self.assertEqual(
            (d_one.allowed, self.executor.jobs_created, self.executor.workload_calls_total),
            (False, 0, 0),
        )
        self.assertIn(
            d_one.reason,
            {DecisionReason.COST_UNKNOWN, DecisionReason.METERING_INVALID},
        )

        # Falsy/truthy non-booleans (0, "", "false", "true", 0.0, []) must NEVER coerce via bool().
        for bad_flag in (0, "", "false", "true", 0.0, []):
            snap_bad = SyntheticMeterSnapshot(
                resources=base_resources,
                paid_feature_active=bad_flag,  # type: ignore[arg-type]
            )
            d_bad = self.guard.execute(ActionKind.JOB_CREATE, snap_bad)
            self.assertFalse(d_bad.allowed, f"non-bool {bad_flag!r} must be rejected")
            self.assertIn(
                d_bad.reason,
                {DecisionReason.COST_UNKNOWN, DecisionReason.METERING_INVALID},
            )
        self.assertEqual(self.executor.jobs_created, 0)
        self.assertEqual(self.executor.workload_calls_total, 0)

        # Explicit True still triggers PAID_FEATURE_DETECTED and halts the guard with 0 calls.
        exec_true = FakeExecutor(self.clock)
        guard_true = SpendGuard(policy=self.policy, clock=self.clock, executor=exec_true)
        d_true = guard_true.execute(
            ActionKind.JOB_CREATE,
            SyntheticMeterSnapshot(resources=base_resources, paid_feature_active=True),
        )
        self.assertFalse(d_true.allowed)
        self.assertEqual(d_true.reason, DecisionReason.PAID_FEATURE_DETECTED)
        self.assertEqual(guard_true.state, GuardState.HALTED)
        self.assertEqual(exec_true.workload_calls_total, 0)

        # ONLY explicit False is accepted as verified inactive.
        d_false = self.guard.execute(
            ActionKind.JOB_CREATE,
            SyntheticMeterSnapshot(resources=base_resources, paid_feature_active=False),
        )
        self.assertTrue(d_false.allowed)
        self.assertEqual(self.executor.jobs_created, 1)
        self.assertEqual(self.executor.workload_calls_total, 1)


if __name__ == "__main__":
    unittest.main()
