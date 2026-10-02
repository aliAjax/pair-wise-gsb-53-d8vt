import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor
from src.repository import Repository


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
LEGACY_KEYS = ["tolling_intervals", "clock_paused", "paused_days_remaining", "delivery_method", "delivery_day", "delivery_confirmed", "evidence_withdrawn", "evidence_overdue", "late_days", "deadline_basis", "allowed_days"]


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _to_evidence_requested(self, reference):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        record = self.service.act(Actor("operator", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        record = self.service.act(Actor("operator", "case_officer"), record["id"], record["version"], "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "delivery_method": "mail", "evidence_request": "补充收入证明"})
        return record

    def _strip_to_legacy(self, record_id, resets=None):
        connection = sqlite3.connect(self.db)
        row = connection.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        payload = json.loads(row[0])
        for key in LEGACY_KEYS:
            payload.pop(key, None)
        if resets:
            payload.update(resets)
        connection.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False, sort_keys=True), record_id))
        connection.commit()
        connection.close()

    def test_backfill_open_interval_for_pending_evidence(self):
        record = self._to_evidence_requested("IMM-39001")
        self._strip_to_legacy(record["id"], {"evidence_due_day": 125})
        Repository(self.db)
        migrated = self.service.get_record(Actor("creator", "intake_officer"), record["id"])
        payload = migrated["payload"]
        self.assertTrue(payload["clock_paused"])
        self.assertEqual(payload["paused_days_remaining"], 15)
        self.assertEqual(payload["allowed_days"], 10)
        self.assertEqual(payload["tolling_intervals"], [{"start_day": 115, "end_day": None, "reason": "evidence_request", "closed_by": None, "migrated": True}])
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        backfills = [event for event in timeline if event["action"] == "legacy_tolling_backfill"]
        self.assertEqual(len(backfills), 1)
        Repository(self.db)
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        self.assertEqual(len([event for event in timeline if event["action"] == "legacy_tolling_backfill"]), 1)
        migrated = self.service.act(Actor("operator", "case_officer"), migrated["id"], migrated["version"], "confirm_delivery", {"delivery_day": 118, "delivery_method": "mail"})
        self.assertEqual(migrated["payload"]["evidence_due_day"], 131)

    def test_backfill_closed_interval_for_responded_case(self):
        record = self._to_evidence_requested("IMM-39002")
        record = self.service.act(Actor("operator", "legal_rep"), record["id"], record["version"], "respond", {"response_day": 120, "documents": ["income_proof"]})
        self._strip_to_legacy(record["id"], {"evidence_due_day": 125, "deadline_day": 130, "days_remaining": 10, "overdue": False})
        Repository(self.db)
        migrated = self.service.get_record(Actor("creator", "intake_officer"), record["id"])
        payload = migrated["payload"]
        self.assertFalse(payload["clock_paused"])
        self.assertEqual(payload["tolling_intervals"][0]["end_day"], 120)
        self.assertEqual(payload["tolling_intervals"][0]["closed_by"], "respond")
        self.assertEqual(payload["deadline_day"], 135)
        self.assertEqual(payload["days_remaining"], 15)
        self.assertFalse(payload["overdue"])
