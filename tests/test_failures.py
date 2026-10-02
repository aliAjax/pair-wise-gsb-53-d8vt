import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
FLOW = [('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']}, 'submitted'), ('request_evidence', 'case_officer', {'evidence_request_day': 115, 'allowed_days': 10, 'delivery_method': 'mail', 'evidence_request': '补充收入证明'}, 'evidence_requested'), ('respond', 'legal_rep', {'response_day': 120, 'documents': ['income_proof']}, 'response_received'), ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': '材料充分'}, 'decided')]


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _to_evidence_requested(self, reference="IMM-29001"):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        record = self.service.act(Actor("operator", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        record = self.service.act(Actor("operator", "case_officer"), record["id"], record["version"], "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "delivery_method": "mail", "evidence_request": "补充收入证明"})
        return record

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "IMM-29001", CREATE_DATA)
        self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])

    def test_withdraw_and_method_change_require_supervisor(self):
        record = self._to_evidence_requested()
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("operator", "case_officer"), record["id"], record["version"], "withdraw_evidence", {"withdraw_day": 120})
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("operator", "legal_rep"), record["id"], record["version"], "change_delivery_method", {"delivery_method": "electronic"})

    def test_confirm_delivery_requires_evidence_state(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        record = self.service.act(Actor("operator", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", "case_officer"), record["id"], record["version"], "confirm_delivery", {"delivery_day": 115})

    def test_concurrent_delivery_confirmation_single_winner(self):
        record = self._to_evidence_requested()
        version = record["version"]
        barrier = threading.Barrier(2)
        won = []
        lost = []

        def confirm(day):
            try:
                barrier.wait(timeout=10)
                won.append(self.service.act(Actor("officer", "case_officer"), record["id"], version, "confirm_delivery", {"delivery_day": day}))
            except Conflict:
                lost.append(day)

        threads = [threading.Thread(target=confirm, args=(118,)), threading.Thread(target=confirm, args=(119,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(won), 1)
        self.assertEqual(len(lost), 1)
        final = self.service.get_record(Actor("creator", "intake_officer"), record["id"])
        self.assertEqual(final["payload"]["evidence_due_day"], won[0]["payload"]["evidence_due_day"])
        self.assertEqual(final["version"], version + 1)
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        confirms = [event for event in timeline if event["action"] == "confirm_delivery"]
        self.assertEqual(len(confirms), 1)
        self.assertEqual(confirms[0]["details"]["calculation"]["evidence_due_day"], final["payload"]["evidence_due_day"])
