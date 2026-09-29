from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from collection_logistics.api import JsonApplication
from collection_logistics.clock import FrozenClock
from collection_logistics.errors import Conflict, Forbidden, InvalidState
from collection_logistics.planning import AllocationRequest, RiskPoint, allocate_capacity, latest_streak, scenario_projection
from collection_logistics.models import ResponseScenario
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
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["observation"]["risk_record_id"], first["observation"]["risk_record_id"])
        self.assertEqual(second["observation"]["source_revision"], "r-23")
        self.assertEqual(second["input_sha256"], first["input_sha256"])

    def test_scenario_requires_constraint_fields_at_creation(self) -> None:
        payload = {"scenario_id": "restart", "name": "雨后开放", "risk_index_drop_percent": "9"}
        with self.assertRaises(Exception):
            self.service.create_scenario("plan", payload)
        payload.update({"risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23"})
        created = self.service.create_scenario("plan", payload)
        self.assertEqual(created["risk_index_constraint"], {
            "risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23",
        })

    def test_run_stops_without_exact_constraint_match(self) -> None:
        # 同一天存在另一指标系列（游客踩踏压力）的观测，但情景要求土壤含水率
        self.risk_record(23, "98")
        self.service.record_risk_record("plan", {"risk_index": "CONGESTION", "duty_date": "2026-09-23", "index_value": "87", "source_revision": "r-23", "observed_at": "2026-09-23T20:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "rain-open", "name": "雨后开放", "risk_index": "HUMIDITY", "source_revision": "missing-rev", "duty_date": "2026-09-23", "risk_index_drop_percent": "0"})
        self.service.approve_scenario("risk", "rain-open", 1)
        with self.assertRaises(InvalidState) as context:
            self.service.run_scenario("plan", "rain-open", "2026-09-23")
        self.assertIn("source_revision=missing-rev", str(context.exception))
        rows = self.connection.execute("SELECT count(*) FROM response_scenario_runs").fetchone()
        self.assertEqual(rows[0], 0)

    def test_run_rejects_wrong_series_even_when_other_series_is_newest(self) -> None:
        # 旧逻辑会取到 duty_date<=? 的任意最新记录（CONGESTION 87）
        self.risk_record(23, "98")
        self.service.record_risk_record("plan", {"risk_index": "CONGESTION", "duty_date": "2026-09-23", "index_value": "87", "source_revision": "r-23", "observed_at": "2026-09-23T21:30:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "rain-open", "name": "雨后开放", "risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23", "risk_index_drop_percent": "0"})
        self.service.approve_scenario("risk", "rain-open", 1)
        run = self.service.run_scenario("plan", "rain-open", "2026-09-23")
        self.assertEqual(run["observation"]["risk_index"], "HUMIDITY")
        self.assertEqual(run["observation"]["index_value"], "98")
        self.assertNotEqual(run["projected_risk_index_cny"], "87.00")

    def test_run_rejects_date_other_than_fixed_duty_date(self) -> None:
        self.risk_record(23, "98")
        self.risk_record(24, "96")
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23", "risk_index_drop_percent": "0"})
        self.service.approve_scenario("risk", "restart", 1)
        with self.assertRaises(InvalidState):
            self.service.run_scenario("plan", "restart", "2026-09-24")

    def test_later_revision_does_not_alter_old_run(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        # 事后补录同日“更正修订”，情景固定的 r-23 仍精确命中旧观测，重放返回同一运行
        self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-23", "index_value": "50", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T23:00:00Z"})
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["input_sha256"], first["input_sha256"])
        self.assertEqual(second["projected_risk_index_cny"], first["projected_risk_index_cny"])
        self.assertEqual(second["observation"]["index_value"], "98")
        self.assertEqual(second["observation"]["source_revision"], "r-23")
        detail = self.service.scenario_run("audit", first["run_id"])
        self.assertEqual(detail["observation"]["index_value"], "98")
        self.assertEqual(detail["projected_risk_index_cny"], first["projected_risk_index_cny"])

    def test_old_run_snapshot_survives_live_catalog_rewrite(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        old_capacity = first["total_projected_capacity"]
        old_inventory = first["demand_adjusted_inventory"]
        # 直接改写路线和库存：旧运行记录中固化的快照与结果保持原样
        self.connection.execute("UPDATE road_corridors SET hourly_capacity='1' WHERE corridor_id='transfer-east-1'")
        self.connection.execute("UPDATE preservation_resource_lots SET available_units='0' WHERE preservation_resource_lot_id='lot-1'")
        detail = self.service.scenario_run("audit", first["run_id"])
        self.assertEqual(detail["total_projected_capacity"], old_capacity)
        self.assertEqual(detail["demand_adjusted_inventory"], old_inventory)
        self.assertEqual(detail["input_snapshot"]["road_corridors"][0]["hourly_capacity"], "100000")
        self.assertEqual(detail["input_snapshot"]["inventory"][0]["available_units"], 60000.0)

    def test_same_snapshot_reproduces_same_output(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
        self.service.approve_scenario("risk", "restart", 1)
        run = self.service.run_scenario("plan", "restart", "2026-09-23")
        snapshot = run["input_snapshot"]
        # 用快照中的同一条观测重新计算，输出与已批准结论完全一致
        reproduced = scenario_projection(
            current_index=Decimal(snapshot["observation"]["index_value"]),
            risk_index_drop_percent=ResponseScenario.from_dict(snapshot["scenario"]["definition"]).risk_index_drop_percent,
            road_corridors=snapshot["road_corridors"],
            inventory=snapshot["inventory"],
            route_capacity_changes=ResponseScenario.from_dict(snapshot["scenario"]["definition"]).route_capacity_changes,
            demand_changes=ResponseScenario.from_dict(snapshot["scenario"]["definition"]).demand_changes,
        )
        self.assertEqual(reproduced["projected_risk_index_cny"], run["projected_risk_index_cny"])
        self.assertEqual(reproduced["total_projected_capacity"], run["total_projected_capacity"])
        self.assertEqual(reproduced["demand_adjusted_inventory"], run["demand_adjusted_inventory"])

    def test_scenario_run_detail_explains_match_and_lists_runs(self) -> None:
        self.risk_record(23, "98")
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index": "HUMIDITY", "source_revision": "r-23", "duty_date": "2026-09-23", "risk_index_drop_percent": "0"})
        self.service.approve_scenario("risk", "restart", 1)
        run = self.service.run_scenario("plan", "restart", "2026-09-23")
        detail = self.service.scenario_run("audit", run["run_id"])
        self.assertEqual(detail["constraint"], run["constraint"])
        self.assertEqual(detail["observation"]["risk_record_id"], run["observation"]["risk_record_id"])
        self.assertTrue(detail["input_snapshot"]["match"]["exact_match"])
        listing = self.service.scenario_runs("audit", "restart")
        self.assertEqual([item["run_id"] for item in listing["runs"]], [run["run_id"]])
        self.assertEqual(listing["runs"][0]["risk_record_id"], run["observation"]["risk_record_id"])

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
