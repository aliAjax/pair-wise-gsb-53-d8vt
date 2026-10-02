import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
FLOW = [('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']}, 'submitted'), ('request_evidence', 'case_officer', {'evidence_request_day': 115, 'allowed_days': 10, 'delivery_method': 'mail', 'evidence_request': '补充收入证明'}, 'evidence_requested'), ('respond', 'legal_rep', {'response_day': 120, 'documents': ['income_proof']}, 'response_received'), ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': '材料充分'}, 'decided')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def _submitted_record(self):
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, _, _ = self.rules.apply_action(record, "submit", {"documents": ["passport", "sponsor_letter"]})
        return {"id": 1, "state": state, "payload": payload}

    def _evidence_record(self):
        record = self._submitted_record()
        state, payload, _, _ = self.rules.apply_action(record, "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "delivery_method": "mail", "evidence_request": "补充收入证明"})
        return {"id": 1, "state": state, "payload": payload}

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["deadline_day"], 130)
        self.assertEqual(prepared["days_remaining"], 20)
        self.assertFalse(prepared["overdue"])
        self.assertEqual(prepared["tolling_intervals"], [])
        self.assertFalse(prepared["clock_paused"])

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary, calculation = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["missing_documents"], [])

    def test_request_evidence_pauses_clock(self):
        record = self._submitted_record()
        state, payload, summary, calculation = self.rules.apply_action(record, "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "delivery_method": "mail", "evidence_request": "补充收入证明"})
        self.assertEqual(state, "evidence_requested")
        self.assertEqual(payload["evidence_due_day"], 128)
        self.assertTrue(payload["clock_paused"])
        self.assertEqual(payload["paused_days_remaining"], 15)
        self.assertEqual(payload["tolling_intervals"], [{"start_day": 115, "end_day": None, "reason": "evidence_request", "closed_by": None}])
        self.assertIn("暂停", calculation["basis"])

    def test_confirm_delivery_recalculates_due_day(self):
        record = self._evidence_record()
        state, payload, summary, calculation = self.rules.apply_action(record, "confirm_delivery", {"delivery_day": 118})
        self.assertEqual(state, "evidence_requested")
        self.assertTrue(payload["delivery_confirmed"])
        self.assertEqual(payload["evidence_due_day"], 131)
        self.assertEqual(calculation["previous_evidence_due_day"], 128)
        self.assertIn("送达确认", calculation["basis"])

    def test_late_response_enters_overdue_and_keeps_interval(self):
        record = self._evidence_record()
        state, payload, _, _ = self.rules.apply_action(record, "confirm_delivery", {"delivery_day": 118})
        record = {"id": 1, "state": state, "payload": payload}
        state, payload, summary, calculation = self.rules.apply_action(record, "respond", {"response_day": 134, "documents": ["income_proof"]})
        self.assertEqual(state, "response_received")
        self.assertTrue(payload["evidence_overdue"])
        self.assertEqual(payload["late_days"], 3)
        self.assertEqual(payload["deadline_day"], 149)
        self.assertEqual(payload["days_remaining"], 15)
        self.assertFalse(payload["clock_paused"])
        self.assertEqual(payload["tolling_intervals"][0]["end_day"], 134)
        self.assertEqual(payload["tolling_intervals"][0]["closed_by"], "respond")
        self.assertIn("逾期3天", summary)

    def test_withdraw_evidence_invalidates_due_day(self):
        record = self._evidence_record()
        state, payload, summary, calculation = self.rules.apply_action(record, "withdraw_evidence", {"withdraw_day": 120})
        self.assertEqual(state, "submitted")
        self.assertIsNone(payload["evidence_due_day"])
        self.assertTrue(payload["evidence_withdrawn"])
        self.assertEqual(payload["deadline_day"], 135)
        self.assertEqual(payload["days_remaining"], 15)
        self.assertEqual(payload["tolling_intervals"][0]["closed_by"], "withdraw_evidence")
        self.assertEqual(calculation["previous_evidence_due_day"], 128)

    def test_change_delivery_method_recalculates(self):
        record = self._evidence_record()
        state, payload, summary, calculation = self.rules.apply_action(record, "change_delivery_method", {"delivery_method": "electronic"})
        self.assertEqual(payload["evidence_due_day"], 125)
        self.assertEqual(payload["delivery_method"], "electronic")
        self.assertEqual(calculation["previous_evidence_due_day"], 128)
        self.assertIn("失效", calculation["basis"])

    def test_response_before_request_day_rejected(self):
        record = self._evidence_record()
        with self.assertRaises(ValidationError):
            self.rules.apply_action(record, "respond", {"response_day": 112, "documents": ["income_proof"]})

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["case_type"] = 'tourist'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)
