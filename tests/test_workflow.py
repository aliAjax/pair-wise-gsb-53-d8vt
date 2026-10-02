import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
FLOW = [('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']}, 'submitted'), ('request_evidence', 'case_officer', {'evidence_request_day': 115, 'allowed_days': 10, 'delivery_method': 'mail', 'evidence_request': '补充收入证明'}, 'evidence_requested'), ('respond', 'legal_rep', {'response_day': 120, 'documents': ['income_proof']}, 'response_received'), ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': '材料充分'}, 'decided')]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _to_evidence_requested(self, reference):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        record = self.service.act(Actor("operator", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        record = self.service.act(Actor("operator", "case_officer"), record["id"], record["version"], "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "delivery_method": "mail", "evidence_request": "补充收入证明"})
        return record

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        self.assertEqual(record["state"], "draft")
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 1)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])

    def test_tolling_recalc_and_late_response(self):
        record = self._to_evidence_requested("IMM-29002")
        self.assertTrue(record["payload"]["clock_paused"])
        self.assertEqual(record["payload"]["paused_days_remaining"], 15)
        record = self.service.act(Actor("operator", "case_officer"), record["id"], record["version"], "confirm_delivery", {"delivery_day": 118})
        self.assertEqual(record["payload"]["evidence_due_day"], 131)
        record = self.service.act(Actor("operator", "legal_rep"), record["id"], record["version"], "respond", {"response_day": 134, "documents": ["income_proof"]})
        payload = record["payload"]
        self.assertTrue(payload["evidence_overdue"])
        self.assertEqual(payload["late_days"], 3)
        self.assertEqual(payload["deadline_day"], 149)
        self.assertEqual(payload["days_remaining"], 15)
        self.assertEqual(payload["tolling_intervals"][0]["end_day"], 134)
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        confirm_event = [event for event in timeline if event["action"] == "confirm_delivery"][0]
        self.assertIn("送达确认", confirm_event["details"]["calculation"]["basis"])
        respond_event = [event for event in timeline if event["action"] == "respond"][0]
        self.assertEqual(respond_event["details"]["calculation"]["late_days"], 3)
        self.assertEqual(respond_event["details"]["calculation"]["deadline_day"], 149)

    def test_withdraw_and_method_change_invalidate_old_deadline(self):
        record = self._to_evidence_requested("IMM-29003")
        record = self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "change_delivery_method", {"delivery_method": "electronic"})
        self.assertEqual(record["payload"]["evidence_due_day"], 125)
        record = self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "withdraw_evidence", {"withdraw_day": 120})
        self.assertEqual(record["state"], "submitted")
        self.assertIsNone(record["payload"]["evidence_due_day"])
        self.assertEqual(record["payload"]["deadline_day"], 135)
        self.assertEqual(record["payload"]["tolling_intervals"][0]["closed_by"], "withdraw_evidence")
        record = self.service.act(Actor("operator", "case_officer"), record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "材料充分"})
        self.assertEqual(record["state"], "decided")
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        withdraw_event = [event for event in timeline if event["action"] == "withdraw_evidence"][0]
        self.assertIn("失效", withdraw_event["details"]["calculation"]["basis"])
