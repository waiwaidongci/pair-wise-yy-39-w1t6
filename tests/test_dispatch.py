import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.http_api import make_handler
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service

WINDOW_1 = ("2026-07-01T00:00:00+00:00", "2026-07-01T06:00:00+00:00")
WINDOW_2 = ("2026-07-01T10:00:00+00:00", "2026-07-01T16:00:00+00:00")


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.crew_a = self.service.register_crew(
            {"name": "抢险一班", "skills": ["seepage"]}, "mgr", "emergency_manager")
        self.crew_b = self.service.register_crew(
            {"name": "抢险二班", "skills": ["seepage", "crack"]}, "mgr", "emergency_manager")
        self.crew_c = self.service.register_crew(
            {"name": "电气班", "skills": ["power"]}, "mgr", "emergency_manager")
        self.pump1 = self.service.register_resource(
            {"name": "1号排水泵", "type": "pump"}, "mgr", "emergency_manager")
        self.pump2 = self.service.register_resource(
            {"name": "2号排水泵", "type": "pump"}, "mgr", "emergency_manager")
        self.vehicle1 = self.service.register_resource(
            {"name": "1号抢险车", "type": "vehicle"}, "mgr", "emergency_manager")
        self.vehicle2 = self.service.register_resource(
            {"name": "2号抢险车", "type": "vehicle"}, "mgr", "emergency_manager")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _confirmed_item(self, severity="major", ref="D-1"):
        item = self.service.create_item(
            {"title": "坝后渗流", "description": "渗水量异常", "severity": severity,
             "quantity": 12, "threshold": 6, "external_ref": ref},
            "creator", "inspector")
        for target in STATES[1:3]:
            item = self.service.transition(
                item["id"], target, item["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        self.assertEqual(item["status"], "defect_confirmed")
        return item

    def _reserve(self, item, pump=None, vehicle=None, window=WINDOW_1, skill="seepage"):
        return self.service.create_dispatch(
            item["id"],
            {"required_skill": skill,
             "pump_ids": [(pump or self.pump1)["id"]],
             "vehicle_ids": [(vehicle or self.vehicle1)["id"]],
             "window_start": window[0], "window_end": window[1]},
            "duty", "dam_engineer")

    def test_skill_match_picks_idle_crew_and_occupies_resources(self):
        item1 = self._confirmed_item(ref="D-1")
        d1 = self._reserve(item1)
        self.assertEqual(d1["status"], "reserved")
        self.assertEqual(d1["crew_id"], self.crew_a["id"])
        self.assertEqual(len(d1["resources"]), 2)
        # 同一时段：一班与泵1、车1已被占用，应自动匹配二班并占住泵2、车2
        item2 = self._confirmed_item("emergency", "D-2")
        d2 = self._reserve(item2, self.pump2, self.vehicle2)
        self.assertEqual(d2["crew_id"], self.crew_b["id"])
        self.assertEqual(d2["status"], "reserved")

    def test_time_conflict_returns_objects_and_saves_nothing(self):
        item1 = self._confirmed_item(ref="D-1")
        self._reserve(item1)
        item2 = self._confirmed_item("emergency", "D-2")
        with self.assertRaises(ConflictError) as caught:
            self._reserve(item2)
        details = caught.exception.details
        kinds = {(d["kind"], d.get("resource_id")) for d in details}
        self.assertIn(("resource", self.pump1["id"]), kinds)
        self.assertIn(("resource", self.vehicle1["id"]), kinds)
        # 二班同样具备seepage且空闲，故班组可匹配；冲突仅来自已占泵车
        self.assertTrue(all(d["kind"] == "resource" for d in details))
        for detail in details:
            self.assertEqual(detail["dispatch_id"], 1)
        # 整笔调度未保存：没有生成第二条调度，泵仍只被调度1占用
        self.assertEqual(len(self.service.list_dispatches("viewer")), 1)
        rows = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM dispatch_resources").fetchone()
        self.assertEqual(rows["n"], 2)

    def test_no_skilled_idle_crew_conflict(self):
        item1 = self._confirmed_item(ref="D-1")
        self._reserve(item1)
        # 让二班（也具备seepage技能）在与一班重叠的时段同样被占住
        pump3 = self.service.register_resource(
            {"name": "3号排水泵", "type": "pump"}, "mgr", "emergency_manager")
        vehicle3 = self.service.register_resource(
            {"name": "3号抢险车", "type": "vehicle"}, "mgr", "emergency_manager")
        item3 = self._confirmed_item("emergency", "D-3")
        self.service.create_dispatch(
            item3["id"],
            {"required_skill": "seepage", "pump_ids": [pump3["id"]],
             "vehicle_ids": [vehicle3["id"]],
             "window_start": "2026-07-01T03:00:00+00:00",
             "window_end": "2026-07-01T05:00:00+00:00"},
            "duty", "dam_engineer")
        # 此时泵2、车2空闲，但没有空闲的seepage班组：返回班组占用，整笔不保存
        item2 = self._confirmed_item("emergency", "D-2")
        with self.assertRaises(ConflictError) as caught:
            self._reserve(item2, self.pump2, self.vehicle2,
                          window=("2026-07-01T02:00:00+00:00",
                                  "2026-07-01T04:00:00+00:00"))
        self.assertEqual(caught.exception.details,
                         [{"kind": "crew", "required_skill": "seepage"}])
        self.assertEqual(
            len(self.service.list_dispatches("viewer", status="reserved")), 2)
        # 电气班具备power技能，且在不冲突的时段可派出
        item4 = self._confirmed_item("emergency", "D-4")
        dispatch = self.service.create_dispatch(
            item4["id"],
            {"required_skill": "power", "pump_ids": [self.pump2["id"]],
             "vehicle_ids": [self.vehicle2["id"]],
             "window_start": WINDOW_2[0], "window_end": WINDOW_2[1]},
            "duty", "dam_engineer")
        self.assertEqual(dispatch["crew_id"], self.crew_c["id"])

    def test_non_overlapping_window_reuses_everything(self):
        item1 = self._confirmed_item(ref="D-1")
        self._reserve(item1, window=WINDOW_1)
        item2 = self._confirmed_item("emergency", "D-2")
        d2 = self._reserve(item2, window=WINDOW_2)
        self.assertEqual(d2["crew_id"], self.crew_a["id"])
        self.assertEqual({r["resource_id"] for r in d2["resources"]},
                         {self.pump1["id"], self.vehicle1["id"]})

    def test_cancel_before_arrival_releases_all_resources(self):
        item1 = self._confirmed_item(ref="D-1")
        d1 = self._reserve(item1)
        cancelled = self.service.dispatch_action(
            d1["id"], {"action": "cancel", "expected_version": d1["version"]},
            "duty", "dam_engineer")
        self.assertEqual(cancelled["status"], "cancelled")
        # 释放后，同一班组与同一组泵车可在同一时段再次被占
        item2 = self._confirmed_item("emergency", "D-2")
        d2 = self._reserve(item2)
        self.assertEqual(d2["crew_id"], self.crew_a["id"])

    def test_cancel_after_arrival_forbidden_only_complete_returns_materials(self):
        item1 = self._confirmed_item(ref="D-1")
        d1 = self._reserve(item1)
        arrived = self.service.dispatch_action(
            d1["id"], {"action": "arrive", "expected_version": d1["version"]},
            "duty", "dam_engineer")
        with self.assertRaises(ConflictError):
            self.service.dispatch_action(
                d1["id"], {"action": "cancel", "expected_version": arrived["version"]},
                "duty", "dam_engineer")
        completed = self.service.dispatch_action(
            d1["id"], {"action": "complete", "expected_version": arrived["version"]},
            "duty", "dam_engineer")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(completed["resources"]), 2)
        self.assertTrue(all(r["returned_at"] for r in completed["resources"]))
        # 办结归还后，资源与班组可在同一时段再次占用
        item2 = self._confirmed_item("emergency", "D-2")
        d2 = self._reserve(item2)
        self.assertEqual(d2["crew_id"], self.crew_a["id"])

    def test_dispatch_lifecycle_guard(self):
        item1 = self._confirmed_item(ref="D-1")
        d1 = self._reserve(item1)
        with self.assertRaises(ConflictError):  # 未到场不能办结
            self.service.dispatch_action(
                d1["id"], {"action": "complete", "expected_version": d1["version"]},
                "duty", "dam_engineer")
        arrived = self.service.dispatch_action(
            d1["id"], {"action": "arrive", "expected_version": d1["version"]},
            "duty", "dam_engineer")
        with self.assertRaises(ConflictError):  # 已到场不能重复到场
            self.service.dispatch_action(
                d1["id"], {"action": "arrive", "expected_version": arrived["version"]},
                "duty", "dam_engineer")
        completed = self.service.dispatch_action(
            d1["id"], {"action": "complete", "expected_version": arrived["version"]},
            "duty", "dam_engineer")
        with self.assertRaises(ConflictError):  # 办结后终态
            self.service.dispatch_action(
                d1["id"], {"action": "cancel", "expected_version": completed["version"]},
                "duty", "dam_engineer")

    def test_version_conflict(self):
        item1 = self._confirmed_item(ref="D-1")
        d1 = self._reserve(item1)
        self.service.dispatch_action(
            d1["id"], {"action": "arrive", "expected_version": d1["version"]},
            "duty", "dam_engineer")
        with self.assertRaises(ConflictError):
            self.service.dispatch_action(
                d1["id"], {"action": "complete", "expected_version": d1["version"]},
                "duty", "dam_engineer")

    def test_only_major_or_emergency_confirmed_needs_dispatch(self):
        item = self.service.create_item(
            {"title": "小缺陷", "description": "观察项", "severity": "minor",
             "quantity": 1, "threshold": 6}, "creator", "inspector")
        for target in STATES[1:3]:
            item = self.service.transition(
                item["id"], target, item["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self._reserve(item)

    def test_permissions(self):
        item1 = self._confirmed_item(ref="D-1")
        with self.assertRaises(PermissionDenied):
            self.service.register_crew(
                {"name": "x", "skills": ["seepage"]}, "p", "dam_engineer")
        with self.assertRaises(PermissionDenied):
            self.service.create_dispatch(
                item1["id"],
                {"required_skill": "seepage", "pump_ids": [self.pump1["id"]],
                 "vehicle_ids": [self.vehicle1["id"]],
                 "window_start": WINDOW_1[0], "window_end": WINDOW_1[1]},
                "p", "inspector")

    def test_validation(self):
        item1 = self._confirmed_item(ref="D-1")
        with self.assertRaises(ValidationError):
            self.service.create_dispatch(
                item1["id"],
                {"required_skill": "seepage", "pump_ids": [],
                 "vehicle_ids": [self.vehicle1["id"]],
                 "window_start": WINDOW_1[0], "window_end": WINDOW_1[1]},
                "duty", "dam_engineer")
        with self.assertRaises(ValidationError):
            self.service.create_dispatch(
                item1["id"],
                {"required_skill": "seepage",
                 "pump_ids": [self.pump1["id"], self.pump1["id"]],
                 "vehicle_ids": [self.vehicle1["id"]],
                 "window_start": WINDOW_1[0], "window_end": WINDOW_1[1]},
                "duty", "dam_engineer")
        with self.assertRaises(ValidationError):
            self.service.create_dispatch(
                item1["id"],
                {"required_skill": "seepage",
                 "pump_ids": [self.vehicle1["id"]],
                 "vehicle_ids": [self.vehicle2["id"]],
                 "window_start": WINDOW_1[0], "window_end": WINDOW_1[1]},
                "duty", "dam_engineer")
        with self.assertRaises(ValidationError):
            self.service.create_dispatch(
                item1["id"],
                {"required_skill": "seepage", "pump_ids": [self.pump1["id"]],
                 "vehicle_ids": [self.vehicle1["id"]],
                 "window_start": "2026-07-01 00:00", "window_end": WINDOW_1[1]},
                "duty", "dam_engineer")
        with self.assertRaises(ValidationError):
            self.service.create_dispatch(
                item1["id"],
                {"required_skill": "seepage", "pump_ids": [self.pump1["id"]],
                 "vehicle_ids": [self.vehicle1["id"]],
                 "window_start": WINDOW_1[1], "window_end": WINDOW_1[0]},
                "duty", "dam_engineer")

    def test_cannot_close_defect_until_dispatch_completed(self):
        item1 = self._confirmed_item(ref="D-1")
        d1 = self._reserve(item1)
        # 缺陷继续流转到verified
        current = self.service.transition(
            item1["id"], "repair", item1["version"], "eng", "dam_engineer")
        current = self.service.transition(
            current["id"], "verified", current["version"], "insp", "inspector")
        with self.assertRaises(ConflictError):
            self.service.transition(
                current["id"], "closed", current["version"], "mgr", "emergency_manager")
        arrived = self.service.dispatch_action(
            d1["id"], {"action": "arrive", "expected_version": d1["version"]},
            "duty", "dam_engineer")
        self.service.dispatch_action(
            d1["id"], {"action": "complete", "expected_version": arrived["version"]},
            "duty", "dam_engineer")
        closed = self.service.transition(
            current["id"], "closed", current["version"], "mgr", "emergency_manager")
        self.assertEqual(closed["status"], "closed")

    def test_concurrent_reservations_atomically_serialized(self):
        item1 = self._confirmed_item(ref="D-1")
        item2 = self._confirmed_item("emergency", "D-2")
        results = []

        def reserve(item):
            try:
                results.append(self._reserve(item))
            except ConflictError as exc:
                results.append(exc)

        t1 = threading.Thread(target=reserve, args=(item1,))
        t2 = threading.Thread(target=reserve, args=(item2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        success = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(success), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(self.service.list_dispatches("viewer", status="reserved")), 1)


class DispatchHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "http.db"))
        self.service = Service(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            self.service, str(Path(__file__).resolve().parent.parent / "static")))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.repo.close(); self.tmp.cleanup()

    def _request(self, method, path, payload=None, role="dam_engineer"):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json",
                     "X-Actor": "duty", "X-Role": role})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_conflict_response_lists_objects(self):
        for rtype, name in (("pump", "1号排水泵"), ("vehicle", "1号抢险车")):
            self._request("POST", "/api/resources", {"name": name, "type": rtype},
                          role="emergency_manager")
        self._request("POST", "/api/crews",
                      {"name": "一班", "skills": ["seepage"]}, role="emergency_manager")

        def confirmed(ref):
            status, item = self._request(
                "POST", "/api/items",
                {"title": ref, "description": "渗流异常", "severity": "major",
                 "quantity": 12, "threshold": 6, "external_ref": ref},
                role="inspector")
            item_id = item["id"]; version = item["version"]
            status, item = self._request(
                "POST", f"/api/items/{item_id}/transition",
                {"target": "inspected", "expected_version": version},
                role="inspector")
            status, item = self._request(
                "POST", f"/api/items/{item_id}/transition",
                {"target": "defect_confirmed", "expected_version": item["version"]},
                role="dam_engineer")
            return item_id

        id1, id2 = confirmed("H-1"), confirmed("H-2")
        payload = {"required_skill": "seepage", "pump_ids": [1],
                   "vehicle_ids": [2], "window_start": WINDOW_1[0],
                   "window_end": WINDOW_1[1]}
        status, first = self._request("POST", f"/api/items/{id1}/dispatches", payload)
        self.assertEqual(status, 201)
        status, second = self._request("POST", f"/api/items/{id2}/dispatches", payload)
        self.assertEqual(status, 409)
        self.assertEqual(second["error"], "ConflictError")
        self.assertIsInstance(second["details"], list)
        # 泵、车与唯一具备seepage技能的班组在该时段均被占用
        self.assertEqual(len(second["details"]), 3)
        self.assertIn({"kind": "crew", "required_skill": "seepage"}, second["details"])


if __name__ == "__main__":
    unittest.main()
