import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.crew_a=self.service.create_crew({"name":"渗流抢修一班","skills":["seepage","crack"]},"planner",'emergency_manager')
        self.crew_b=self.service.create_crew({"name":"位移抢修二班","skills":["displacement"]},"planner",'emergency_manager')
        self.pump=self.service.create_equipment({"kind":"pump","name":"排水泵-1"},"planner",'emergency_manager')
        self.vehicle=self.service.create_equipment({"kind":"vehicle","name":"抢险车-1"},"planner",'emergency_manager')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _confirmed_item(self,severity='emergency',ref="DP-1"):
        item=self.service.create_item({"title":"坝后渗流异常","description":"seepage behind dam","severity":severity,"quantity":9,"threshold":3,"external_ref":ref},"creator",'inspector')
        for target in STATES[1:3]: item=self.service.transition(item["id"],target,item["version"],"reviewer",TRANSITION_ROLES[target][0])
        return item
    def _window(self,start="2026-09-26T08:00:00Z",end="2026-09-26T12:00:00Z"):
        return {"skill":"seepage","start_at":start,"end_at":end,"pump_ids":[self.pump["id"]],"vehicle_ids":[self.vehicle["id"]]}
    def test_dispatch_requires_major_or_emergency_confirmed(self):
        minor=self.service.create_item({"title":"minor","description":"minor defect","severity":'minor',"quantity":1,"threshold":2,"external_ref":"DP-M"},"creator",'inspector')
        with self.assertRaises(ValueError): self.service.create_dispatch(minor["id"],self._window(),"planner",'emergency_manager')
        planned=self.service.create_item({"title":"planned","description":"not confirmed yet","severity":'major',"quantity":9,"threshold":3,"external_ref":"DP-P"},"creator",'inspector')
        with self.assertRaises(ConflictError): self.service.create_dispatch(planned["id"],self._window(),"planner",'emergency_manager')
    def test_skill_matching_and_resource_reservation(self):
        item=self._confirmed_item()
        dispatch=self.service.create_dispatch(item["id"],self._window(),"planner",'emergency_manager')
        self.assertEqual(dispatch["crew_id"],self.crew_a["id"])
        self.assertEqual(dispatch["status"],"scheduled")
        self.assertEqual({r["equipment_id"] for r in dispatch["resources"]},{self.pump["id"],self.vehicle["id"]})
        with self.assertRaises(ValidationError): self.service.create_dispatch(item["id"],{"skill":"displacement","start_at":"2026-09-27T08:00:00Z","end_at":"2026-09-27T12:00:00Z","crew_id":self.crew_a["id"]},"planner",'emergency_manager')
    def test_conflict_returns_occupiers_and_saves_nothing(self):
        item=self._confirmed_item()
        first=self.service.create_dispatch(item["id"],self._window(),"planner",'emergency_manager')
        other=self._confirmed_item(ref="DP-2")
        with self.assertRaises(ConflictError) as ctx:
            self.service.create_dispatch(other["id"],self._window(start="2026-09-26T10:00:00Z",end="2026-09-26T14:00:00Z"),"planner",'emergency_manager')
        conflicts=ctx.exception.details["conflicts"]
        self.assertEqual({c["resource_type"] for c in conflicts},{"crew","pump","vehicle"})
        self.assertTrue(all(c["dispatch_id"]==first["id"] for c in conflicts))
        self.assertEqual(self.service.list_dispatches(other["id"],"viewer"),[])
        free=self.service.create_dispatch(other["id"],self._window(start="2026-09-26T12:00:00Z",end="2026-09-26T16:00:00Z"),"planner",'emergency_manager')
        self.assertEqual(free["status"],"scheduled")
    def test_cancel_before_arrival_releases_resources(self):
        item=self._confirmed_item()
        dispatch=self.service.create_dispatch(item["id"],self._window(),"planner",'emergency_manager')
        cancelled=self.service.cancel_dispatch(dispatch["id"],"planner",'emergency_manager')
        self.assertEqual(cancelled["status"],"cancelled")
        other=self._confirmed_item(ref="DP-2")
        again=self.service.create_dispatch(other["id"],self._window(),"planner",'emergency_manager')
        self.assertEqual(again["status"],"scheduled")
    def test_after_arrival_only_completion_returns_equipment(self):
        item=self._confirmed_item()
        dispatch=self.service.create_dispatch(item["id"],self._window(),"planner",'emergency_manager')
        with self.assertRaises(ConflictError): self.service.complete_dispatch(dispatch["id"],"lead",'dam_engineer')
        arrived=self.service.arrive_dispatch(dispatch["id"],"lead",'dam_engineer')
        self.assertEqual(arrived["status"],"arrived")
        with self.assertRaises(ConflictError): self.service.cancel_dispatch(dispatch["id"],"planner",'emergency_manager')
        done=self.service.complete_dispatch(dispatch["id"],"lead",'dam_engineer')
        self.assertEqual(done["status"],"completed")
        self.assertTrue(all(r["returned"]==1 for r in done["resources"]))
        other=self._confirmed_item(ref="DP-2")
        reused=self.service.create_dispatch(other["id"],self._window(),"planner",'emergency_manager')
        self.assertEqual(reused["status"],"scheduled")
    def test_close_blocked_until_dispatch_completed(self):
        item=self._confirmed_item()
        dispatch=self.service.create_dispatch(item["id"],self._window(),"planner",'emergency_manager')
        for target in STATES[3:-1]: item=self.service.transition(item["id"],target,item["version"],"reviewer",TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError): self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.service.cancel_dispatch(dispatch["id"],"planner",'emergency_manager')
        with self.assertRaises(ConflictError): self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        dispatch=self.service.create_dispatch(item["id"],self._window(),"planner",'emergency_manager')
        self.service.arrive_dispatch(dispatch["id"],"lead",'dam_engineer')
        self.service.complete_dispatch(dispatch["id"],"lead",'dam_engineer')
        closed=self.service.transition(item["id"],STATES[-1],item["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"],STATES[-1])
    def test_dispatch_permissions(self):
        item=self._confirmed_item()
        with self.assertRaises(PermissionDenied): self.service.create_dispatch(item["id"],self._window(),"intruder",'viewer')
        with self.assertRaises(PermissionDenied): self.service.create_crew({"name":"x","skills":["seepage"]},"intruder",'inspector')
        with self.assertRaises(PermissionDenied): self.service.create_equipment({"kind":"pump","name":"x"},"intruder",'inspector')
if __name__=="__main__": unittest.main()
