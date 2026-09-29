from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from collection_logistics.api import JsonApplication
from collection_logistics.clock import FrozenClock
from collection_logistics.errors import Conflict, Forbidden, InvalidState
from collection_logistics.planning import AllocationRequest, RiskPoint, allocate_capacity, latest_streak
from collection_logistics.service import CollectionLogisticsService
from collection_logistics.risk import DemandBucket, inventory_coverage, mark_to_risk, traffic_gap


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            RiskPoint("2026-09-18", Decimal("108")),
            RiskPoint("2026-09-19", Decimal("105")),
            RiskPoint("2026-09-20", Decimal("102")),
            RiskPoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["dispatch_id"], "first")
        self.assertEqual(rows[0]["allocated_units"], "70.000")
        self.assertEqual(rows[1]["allocated_units"], "30.000")

    def test_inventory_coverage_and_traffic_gap(self) -> None:
        coverage = inventory_coverage(
            [{"center_id": "receiving-vault", "preservation_resource_kind": "tow-truck", "available_units": "250"}],
            [DemandBucket("receiving-vault", "tow-truck", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = traffic_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["traffic_gap"], "30.000")

    def test_mark_to_risk_groups_deterministically(self) -> None:
        result = mark_to_risk(
            [{"position_id": "p1", "risk_index": "HUMIDITY", "quantity_units": "100", "baseline_value": "105"}],
            {"HUMIDITY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class CollectionLogisticsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"center_id": "collection-east", "name": "北部标本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
        self.service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
        self.service.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def risk_record(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": f"2026-09-{day}", "index_value": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_risk_record_revisions_preserve_history(self) -> None:
        first = self.risk_record(23, "98")
        second = self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-23", "index_value": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["risk_record_id"], second["risk_record_id"])
        rows = self.connection.execute("SELECT * FROM risk_index_risk_records ORDER BY risk_record_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_risk_record_id"], rows[0]["risk_record_id"])

    def test_dispatch_request_replay_and_payload_conflict(self) -> None:
        payload = {"dispatch_id": "nom-1", "corridor_id": "transfer-east-1", "specimen_event_id": "herbarium-room", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_dispatch("dispatch", payload)
        self.assertEqual(first, self.service.submit_dispatch("dispatch", payload))
        changed = dict(payload, requested_units="81000")
        with self.assertRaises(Conflict):
            self.service.submit_dispatch("dispatch", changed)

    def test_outage_reduces_allocation_and_deployment_consumes_inventory(self) -> None:
        self.service.announce_restriction("risk", "transfer-east-1", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_dispatch("dispatch", {"dispatch_id": f"nom-{number}", "corridor_id": "transfer-east-1", "specimen_event_id": f"specimen_event-{number}", "duty_date": "2026-09-25", "requested_units": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "transfer-east-1", "2026-09-25")
        self.assertEqual(allocation["available_units"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_units"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        deployment = self.service.dispatch_deployment("dispatch", "deployment-1", "nom-1", "lot-1", 2)
        self.assertEqual(deployment["deployed_units"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_units"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}, "observation": {"risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart")
        second = self.service.run_scenario("plan", "restart")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def _scenario_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "scenario_id": "trail-reopen",
            "name": "雨后步道开放",
            "risk_index_drop_percent": "0",
            "route_capacity_changes": {},
            "demand_changes": {},
            "observation": {"risk_index": "HUMIDITY", "source_revision": "soil-rev-1", "duty_date": "2026-09-26"},
        }
        payload.update(overrides)
        return payload

    def _approve(self, payload: dict[str, object]) -> None:
        self.service.create_scenario("plan", payload)
        self.service.approve_scenario("risk", str(payload["scenario_id"]), 1)

    def test_run_uses_bound_series_not_same_day_other_indicator(self) -> None:
        # 同一天两类指标都更新：踩踏压力 87 与土壤含水率 62。
        self.service.record_risk_record("plan", {"risk_index": "CONGESTION", "duty_date": "2026-09-26", "index_value": "87", "source_revision": "trample-rev-1", "observed_at": "2026-09-26T08:00:00Z"})
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-26", "index_value": "62", "source_revision": "soil-rev-1", "observed_at": "2026-09-26T07:30:00Z"})
        self._approve(self._scenario_payload())
        result = self.service.run_scenario("plan", "trail-reopen")
        # 必须引用土壤含水率观测，而不是踩踏压力 87。
        self.assertEqual(result["observation"]["risk_index"], "HUMIDITY")
        self.assertEqual(result["observation"]["index_value"], "62")
        self.assertEqual(result["observation"]["source_revision"], "soil-rev-1")
        self.assertTrue(result["selection"]["matched"])
        self.assertEqual(result["projected_risk_index_cny"], "62.00")

    def test_run_stops_when_series_does_not_match(self) -> None:
        # 现场只登记了踩踏压力，而情景要求土壤含水率：必须明确停止。
        self.service.record_risk_record("plan", {"risk_index": "CONGESTION", "duty_date": "2026-09-26", "index_value": "87", "source_revision": "trample-rev-1", "observed_at": "2026-09-26T08:00:00Z"})
        self._approve(self._scenario_payload())
        with self.assertRaises(InvalidState):
            self.service.run_scenario("plan", "trail-reopen")
        blocked = self.connection.execute(
            "SELECT event_type,payload_json FROM traffic_audit_events WHERE entity_type='scenario' "
            "ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(blocked["event_type"], "scenario.execution_blocked")
        self.assertIn("no_matching_observation", blocked["payload_json"])

    def test_run_stops_when_revision_or_date_does_not_match(self) -> None:
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-26", "index_value": "62", "source_revision": "soil-rev-2", "observed_at": "2026-09-26T09:00:00Z"})
        self._approve(self._scenario_payload())
        with self.assertRaises(InvalidState):
            self.service.run_scenario("plan", "trail-reopen")

    def test_later_revision_and_backfill_do_not_change_old_run(self) -> None:
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-26", "index_value": "62", "source_revision": "soil-rev-1", "observed_at": "2026-09-26T07:30:00Z"})
        self._approve(self._scenario_payload())
        first = self.service.run_scenario("plan", "trail-reopen")
        self.assertEqual(first["observation"]["index_value"], "62")
        # 同日发布修正修订，且补录一条更早日期的观测：旧结果必须保持不变。
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-26", "index_value": "55", "source_revision": "soil-rev-2", "observed_at": "2026-09-26T10:00:00Z"})
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-25", "index_value": "70", "source_revision": "soil-rev-0", "observed_at": "2026-09-25T08:00:00Z"})
        replayed = self.service.run_scenario("plan", "trail-reopen")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["run_id"], first["run_id"])
        self.assertEqual(replayed["observation"]["index_value"], "62")
        detail = self.service.scenario_run("audit", first["run_id"])
        self.assertEqual(detail["observation"]["risk_record_id"], first["observation"]["risk_record_id"])
        self.assertEqual(detail["input_sha256"], first["input_sha256"])

    def test_snapshot_reproduces_identical_output(self) -> None:
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-26", "index_value": "62", "source_revision": "soil-rev-1", "observed_at": "2026-09-26T07:30:00Z"})
        self._approve(self._scenario_payload(risk_index_drop_percent="10"))
        run = self.service.run_scenario("plan", "trail-reopen")
        verification = self.service.verify_scenario_run("audit", run["run_id"])
        self.assertTrue(verification["valid"])
        self.assertTrue(verification["checks"]["result_reproduces"])
        self.assertEqual(verification["recomputed_result"], verification["stored_result"])
        # 篡改快照后复算必须失败。
        self.connection.execute(
            "UPDATE response_scenario_runs SET input_snapshot_json=json_set(input_snapshot_json,'$.observation.index_value','1') WHERE run_id=?",
            (run["run_id"],),
        )
        tampered = self.service.verify_scenario_run("audit", run["run_id"])
        self.assertFalse(tampered["valid"])
        self.assertFalse(tampered["checks"]["snapshot_hash_matches"])

    def test_custom_indicator_series_is_supported(self) -> None:
        self.service.record_risk_record("plan", {"risk_index": "CUSTOM:SOIL_MOISTURE", "duty_date": "2026-09-26", "index_value": "62", "source_revision": "soil-rev-1", "observed_at": "2026-09-26T07:30:00Z"})
        payload = self._scenario_payload(
            observation={"risk_index": "CUSTOM:SOIL_MOISTURE", "source_revision": "soil-rev-1", "duty_date": "2026-09-26"}
        )
        self._approve(payload)
        result = self.service.run_scenario("plan", "trail-reopen")
        self.assertEqual(result["observation"]["risk_index"], "CUSTOM:SOIL_MOISTURE")

    def test_scenario_run_api_exposes_selection_and_snapshot(self) -> None:
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-26", "index_value": "62", "source_revision": "soil-rev-1", "observed_at": "2026-09-26T07:30:00Z"})
        self._approve(self._scenario_payload())
        app = JsonApplication(self.service)
        run = self.service.run_scenario("plan", "trail-reopen")
        detail = app.handle("GET", f"/scenarios/runs/{run['run_id']}", {"X-Actor-Id": "audit"})
        self.assertEqual(detail.status, 200)
        self.assertEqual(detail.body["observation"]["index_value"], "62")
        self.assertEqual(detail.body["observation_selector"]["rule"], "exact_match_on_series_revision_and_duty_date")
        verify = app.handle("POST", f"/scenarios/runs/{run['run_id']}/verify", {"X-Actor-Id": "audit"}, b"{}")
        self.assertEqual(verify.status, 200)
        self.assertTrue(verify.body["valid"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE traffic_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/risk_records/summary/HUMIDITY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
