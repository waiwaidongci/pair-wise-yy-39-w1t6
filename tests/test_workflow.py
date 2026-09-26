import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _dispatch(self, item, skill="seepage"):
        crew=self.service.register_crew({"name":"抢修一班","skills":[skill]},"mgr",'emergency_manager')
        pump=self.service.register_resource({"name":"1号排水泵","type":"pump"},"mgr",'emergency_manager')
        vehicle=self.service.register_resource({"name":"1号抢险车","type":"vehicle"},"mgr",'emergency_manager')
        dispatch=self.service.create_dispatch(item["id"],{"required_skill":skill,"pump_ids":[pump["id"]],"vehicle_ids":[vehicle["id"]],"window_start":"2026-07-01T00:00:00+00:00","window_end":"2026-07-01T06:00:00+00:00"},"duty",'dam_engineer')
        dispatch=self.service.dispatch_action(dispatch["id"],{"action":"arrive","expected_version":dispatch["version"]},"duty",'dam_engineer')
        dispatch=self.service.dispatch_action(dispatch["id"],{"action":"complete","expected_version":dispatch["version"]},"duty",'dam_engineer')
        self.assertTrue(all(r["returned_at"] for r in dispatch["resources"]))
    def test_complete_workflow_and_audit(self):
        item=self.service.create_item({"title":"workflow item","description":"complete business flow","severity":'major',"quantity":12,"threshold":6,"external_ref":"WF-1"},"creator",'inspector')
        self.assertEqual(item["status"],STATES[0])
        self.service.add_record(item["id"],{"kind":"evidence","detail":"evidence registered","status":"closed","external_ref":"EV-1"},"recorder",'inspector')
        current=item
        for index,target in enumerate(STATES[1:]):
            if index==2: self._dispatch(current)
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"],STATES[-1])
        self.assertEqual(len(self.service.list_records(current["id"],"viewer")),1)
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(STATES)+1); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
