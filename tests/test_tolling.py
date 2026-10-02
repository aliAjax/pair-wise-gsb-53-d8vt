import threading
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter']}
LEGACY_BASE = {'applicant_id': 'A-901', 'case_type': 'work', 'received_day': 100, 'deadline_days': 30, 'deadline_day': 130, 'representation_active': True, 'required_documents': ['passport'], 'submitted_documents': ['passport'], 'missing_documents': [], 'evidence_request_day': 115, 'evidence_due_day': 125, 'evidence_request': '补充收入证明'}


class TollingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.viewer = Actor("creator", "intake_officer")

    def tearDown(self):
        self.temp.cleanup()

    def _submitted_case(self):
        record = self.service.create(self.viewer, "IMM-30001", CREATE_DATA)
        return self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})

    def _evidence_case(self, request_day=115, allowed_days=10):
        record = self._submitted_case()
        return self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "request_evidence", {"evidence_request_day": request_day, "allowed_days": allowed_days, "evidence_request": "补充收入证明"})

    def test_clock_pauses_when_evidence_requested(self):
        record = self._evidence_case()
        basis = record["payload"]["deadline_basis"]
        self.assertTrue(basis["clock_stopped"])
        self.assertEqual(basis["days_remaining"], 15)  # 130 - 115，剩余天数冻结
        self.assertEqual(record["payload"]["evidence_due_day"], 125)  # 送达确认前的临时期限
        interval = record["payload"]["tolling_intervals"][0]
        self.assertIsNone(interval["end_day"])
        self.assertFalse(interval["annulled"])

    def test_confirm_delivery_recalculates_by_method(self):
        record = self._evidence_case()
        record = self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "confirm_delivery", {"delivery_day": 118, "delivery_method": "mail"})
        payload = record["payload"]
        self.assertEqual(payload["evidence_due_day"], 131)  # 118 + 10 + 邮寄3天
        self.assertTrue(payload["evidence_delivery_confirmed"])
        basis = payload["deadline_basis"]
        self.assertEqual(basis["delivery_extra_days"], 3)
        self.assertEqual(basis["recalculation"]["previous_evidence_due_day"], 125)
        timeline = self.service.timeline(self.viewer, record["id"])
        self.assertEqual(timeline[-1]["details"]["basis"], basis)  # 审计时间线显示同一份计算依据

    def test_late_response_enters_overdue_and_keeps_interval(self):
        record = self._evidence_case()
        record = self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "confirm_delivery", {"delivery_day": 118, "delivery_method": "mail"})
        record = self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "respond", {"response_day": 135, "documents": ["income_proof"]})
        payload = record["payload"]
        self.assertEqual(record["state"], "response_received")  # 晚到材料被接受
        self.assertTrue(payload["late_response"])
        self.assertTrue(payload["overdue"])
        interval = payload["tolling_intervals"][0]
        self.assertEqual((interval["start_day"], interval["end_day"]), (115, 135))  # 停表区间留在记录里
        basis = payload["deadline_basis"]
        self.assertEqual(basis["tolled_days"], 20)
        self.assertEqual(basis["effective_deadline_day"], 150)
        self.assertEqual(basis["days_remaining"], 15)

    def test_withdraw_restores_original_deadline(self):
        record = self._evidence_case()
        record = self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "withdraw_evidence", {"withdraw_day": 120})
        payload = record["payload"]
        self.assertEqual(record["state"], "submitted")
        self.assertIsNone(payload["evidence_due_day"])  # 旧期限立即失效
        interval = payload["tolling_intervals"][0]
        self.assertTrue(interval["annulled"])  # 已发生的停表区间保留
        basis = payload["deadline_basis"]
        self.assertEqual(basis["effective_deadline_day"], 130)
        self.assertEqual(basis["days_remaining"], 10)  # 130 - 120，恢复原期限
        self.assertEqual(basis["recalculation"]["restored_deadline_day"], 130)

    def test_return_evidence_restores_original_deadline(self):
        record = self._evidence_case()
        record = self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "return_evidence", {"return_day": 119})
        self.assertEqual(record["state"], "submitted")
        self.assertEqual(record["payload"]["deadline_basis"]["effective_deadline_day"], 130)

    def test_update_delivery_invalidates_previous_deadline(self):
        record = self._evidence_case()
        record = self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "confirm_delivery", {"delivery_day": 118, "delivery_method": "mail"})
        record = self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "update_delivery", {"delivery_method": "electronic"})
        payload = record["payload"]
        self.assertEqual(payload["evidence_due_day"], 128)  # 118 + 10 + 电子0天
        recalculation = payload["deadline_basis"]["recalculation"]
        self.assertEqual(recalculation["previous_evidence_due_day"], 131)
        self.assertEqual(recalculation["previous_delivery_method"], "mail")

    def test_concurrent_delivery_confirmation_single_winner(self):
        record = self._evidence_case()
        record_id, version = record["id"], record["version"]
        outcomes = {"confirmed": [], "conflict": []}

        def confirm(user):
            try:
                self.service.act(Actor(user, "case_officer"), record_id, version, "confirm_delivery", {"delivery_day": 118, "delivery_method": "mail"})
                outcomes["confirmed"].append(user)
            except Conflict:
                outcomes["conflict"].append(user)

        threads = [threading.Thread(target=confirm, args=("op-%d" % i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(outcomes["confirmed"]), 1)  # 只接受先到版本
        self.assertEqual(len(outcomes["conflict"]), 1)  # 后到者拿到版本冲突
        saved = self.service.get_record(self.viewer, record_id)
        self.assertEqual(saved["version"], version + 1)
        confirmations = [event for event in self.service.timeline(self.viewer, record_id) if event["action"] == "confirm_delivery"]
        self.assertEqual(len(confirmations), 1)
        # 期限与审计时间线同源：记录中的计算依据与审计事件一致
        self.assertEqual(confirmations[0]["details"]["basis"], saved["payload"]["deadline_basis"])

    def test_stale_delivery_confirmation_rejected(self):
        record = self._evidence_case()
        version = record["version"]
        self.service.act(Actor("op-1", "case_officer"), record["id"], version, "confirm_delivery", {"delivery_day": 118, "delivery_method": "mail"})
        with self.assertRaises(Conflict):
            self.service.act(Actor("op-2", "case_officer"), record["id"], version, "confirm_delivery", {"delivery_day": 118, "delivery_method": "mail"})

    def test_delivery_roles_are_restricted(self):
        record = self._evidence_case()
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "confirm_delivery", {"delivery_day": 118, "delivery_method": "mail"})
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("officer", "case_officer"), record["id"], record["version"], "withdraw_evidence", {"withdraw_day": 120})

    def test_legacy_records_are_backfilled_on_upgrade(self):
        legacy_payload = dict(LEGACY_BASE, response_day=120, days_remaining=10, overdue=False)
        legacy = self.service.repository.create("IMM-LEGACY-1", "response_received", legacy_payload, "old-system")
        self.assertEqual(self.service.upgrade_legacy_records(), 1)
        saved = self.service.get_record(self.viewer, legacy["id"])
        interval = saved["payload"]["tolling_intervals"][0]
        self.assertEqual((interval["start_day"], interval["end_day"]), (115, 120))
        self.assertTrue(interval["migrated"])
        basis = saved["payload"]["deadline_basis"]
        self.assertEqual(basis["tolled_days"], 5)
        self.assertEqual(basis["effective_deadline_day"], 135)
        timeline = self.service.timeline(self.viewer, legacy["id"])
        self.assertEqual(timeline[-1]["action"], "migrated")
        self.assertEqual(timeline[-1]["details"]["basis"], basis)
        self.assertEqual(self.service.upgrade_legacy_records(), 0)  # 重复升级不再补齐

    def test_legacy_open_evidence_gets_open_interval(self):
        legacy_payload = dict(LEGACY_BASE, response_day=110, days_remaining=20, overdue=False)
        legacy = self.service.repository.create("IMM-LEGACY-2", "evidence_requested", legacy_payload, "old-system")
        self.assertEqual(self.service.upgrade_legacy_records(), 1)
        saved = self.service.get_record(self.viewer, legacy["id"])
        interval = saved["payload"]["tolling_intervals"][0]
        self.assertIsNone(interval["end_day"])  # 补件仍在进行，停表区间保持打开
        basis = saved["payload"]["deadline_basis"]
        self.assertTrue(basis["clock_stopped"])
        self.assertEqual(basis["days_remaining"], 15)  # 130 - 115

    def test_legacy_late_response_marked_overdue_on_upgrade(self):
        legacy_payload = dict(LEGACY_BASE, response_day=140, days_remaining=-10, overdue=True)
        legacy = self.service.repository.create("IMM-LEGACY-3", "response_received", legacy_payload, "old-system")
        self.service.upgrade_legacy_records()
        saved = self.service.get_record(self.viewer, legacy["id"])
        self.assertTrue(saved["payload"]["late_response"])  # 140 > 125，升级时识别逾期回应
        self.assertTrue(saved["payload"]["deadline_basis"]["overdue"])
