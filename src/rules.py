"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'confirm_delivery': {'case_officer'}, 'change_delivery_method': {'supervisor'}, 'withdraw_evidence': {'supervisor'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'confirm_delivery': {'evidence_requested': 'evidence_requested'}, 'change_delivery_method': {'evidence_requested': 'evidence_requested'}, 'withdraw_evidence': {'evidence_requested': 'submitted'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}
DELIVERY_METHODS = ['mail', 'electronic', 'personal']
DELIVERY_LAG_DAYS = {'mail': 3, 'electronic': 0, 'personal': 0}
DELIVERY_METHOD_LABELS = {'mail': '邮寄', 'electronic': '电子', 'personal': '当面'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def delivery_lag(self, method: Optional[str]) -> int:
        return DELIVERY_LAG_DAYS.get(method, 0)

    def _paused_remaining(self, p: Dict[str, Any]) -> int:
        paused = p.get("paused_days_remaining")
        if paused is None:
            paused = int(p["deadline_day"]) - int(p["evidence_request_day"])
        return int(paused)

    def _allowed_days(self, p: Dict[str, Any]) -> int:
        allowed = p.get("allowed_days")
        if allowed is None:
            allowed = int(p["evidence_due_day"]) - int(p["evidence_request_day"])
        return int(allowed)

    @staticmethod
    def _close_open_interval(intervals: List[Dict[str, Any]], end_day: int, closed_by: str) -> Dict[str, Any]:
        for interval in reversed(intervals):
            if interval.get("end_day") is None:
                interval["end_day"] = end_day
                interval["closed_by"] = closed_by
                return interval
        raise Conflict("没有进行中的停表区间")

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        p["tolling_intervals"] = []
        p["clock_paused"] = False
        p["paused_days_remaining"] = None
        p["delivery_method"] = None
        p["delivery_day"] = None
        p["delivery_confirmed"] = False
        p["evidence_withdrawn"] = False
        p["evidence_overdue"] = False
        p["late_days"] = 0
        p["deadline_basis"] = "初始期限：受理日第%s日+法定%s天=第%s日" % (int(p["received_day"]), int(p["deadline_days"]), p["deadline_day"])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Optional[Dict[str, Any]]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        calculation: Optional[Dict[str, Any]] = None
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", int(p["response_day"]))
            allowed_days = integer(data, "allowed_days", 1)
            method = choice(data, "delivery_method", DELIVERY_METHODS)
            lag = self.delivery_lag(method)
            due_day = request_day + lag + allowed_days
            paused = int(p["deadline_day"]) - request_day
            intervals = [dict(item) for item in p.get("tolling_intervals", [])]
            intervals.append({"start_day": request_day, "end_day": None, "reason": "evidence_request", "closed_by": None})
            basis = "补件发出：主期限于第%s日暂停，暂停时剩余%s天；补件期限暂按发出日计算=第%s日+%s送达%s天+补件%s天=第%s日，送达确认后重算" % (request_day, paused, request_day, DELIVERY_METHOD_LABELS[method], lag, allowed_days, due_day)
            changes["evidence_request_day"] = request_day
            changes["allowed_days"] = allowed_days
            changes["delivery_method"] = method
            changes["delivery_day"] = None
            changes["delivery_confirmed"] = False
            changes["evidence_due_day"] = due_day
            changes["evidence_request"] = text(data, "evidence_request")
            changes["evidence_withdrawn"] = False
            changes["evidence_overdue"] = False
            changes["late_days"] = 0
            changes["clock_paused"] = True
            changes["paused_days_remaining"] = paused
            changes["tolling_intervals"] = intervals
            changes["deadline_basis"] = basis
            summary = "补件要求已发出，主期限暂停"
            calculation = {"basis": basis, "deadline_day": int(p["deadline_day"]), "paused_days_remaining": paused, "evidence_due_day": due_day, "delivery_method": method, "delivery_lag_days": lag, "allowed_days": allowed_days, "delivery_confirmed": False, "tolling_intervals": intervals}
        elif action == "confirm_delivery":
            delivery_day = integer(data, "delivery_day", int(p["evidence_request_day"]))
            if "delivery_method" in data:
                method = choice(data, "delivery_method", DELIVERY_METHODS)
            else:
                method = p.get("delivery_method")
                if method not in DELIVERY_METHODS:
                    raise ValidationError("送达方式未确认，请提供delivery_method")
            lag = self.delivery_lag(method)
            allowed_days = self._allowed_days(p)
            old_due = p.get("evidence_due_day")
            due_day = delivery_day + lag + allowed_days
            basis = "送达确认：补件期限=送达日第%s日+%s送达%s天+补件%s天=第%s日" % (delivery_day, DELIVERY_METHOD_LABELS[method], lag, allowed_days, due_day)
            if old_due is not None and int(old_due) != due_day:
                basis += "；原暂定补件期限第%s日失效" % int(old_due)
            changes["delivery_day"] = delivery_day
            changes["delivery_method"] = method
            changes["delivery_confirmed"] = True
            changes["evidence_due_day"] = due_day
            changes["deadline_basis"] = basis
            summary = "送达已确认，补件期限重算"
            calculation = {"basis": basis, "delivery_day": delivery_day, "delivery_method": method, "delivery_lag_days": lag, "allowed_days": allowed_days, "previous_evidence_due_day": old_due, "evidence_due_day": due_day, "tolling_intervals": p.get("tolling_intervals", [])}
        elif action == "change_delivery_method":
            method = choice(data, "delivery_method", DELIVERY_METHODS)
            base_day = int(p["delivery_day"]) if p.get("delivery_confirmed") and p.get("delivery_day") is not None else int(p["evidence_request_day"])
            lag = self.delivery_lag(method)
            allowed_days = self._allowed_days(p)
            old_due = p.get("evidence_due_day")
            old_method = p.get("delivery_method")
            due_day = base_day + lag + allowed_days
            basis = "送达方式由%s改为%s：原补件期限第%s日立即失效，新补件期限=第%s日+%s送达%s天+补件%s天=第%s日" % (DELIVERY_METHOD_LABELS.get(old_method, "未确认"), DELIVERY_METHOD_LABELS[method], old_due, base_day, DELIVERY_METHOD_LABELS[method], lag, allowed_days, due_day)
            changes["delivery_method"] = method
            changes["evidence_due_day"] = due_day
            changes["deadline_basis"] = basis
            summary = "送达方式已变更，旧补件期限失效并重算"
            calculation = {"basis": basis, "previous_delivery_method": old_method, "delivery_method": method, "previous_evidence_due_day": old_due, "evidence_due_day": due_day, "delivery_lag_days": lag, "allowed_days": allowed_days, "base_day": base_day, "tolling_intervals": p.get("tolling_intervals", [])}
        elif action == "withdraw_evidence":
            withdraw_day = integer(data, "withdraw_day", int(p["evidence_request_day"]))
            paused = self._paused_remaining(p)
            intervals = [dict(item) for item in p.get("tolling_intervals", [])]
            closed = self._close_open_interval(intervals, withdraw_day, "withdraw_evidence")
            old_due = p.get("evidence_due_day")
            new_deadline = withdraw_day + paused
            basis = "补件撤回：原补件期限第%s日立即失效；停表区间第%s-%s日保留；主期限恢复，新截止日=撤回日第%s日+暂停时剩余%s天=第%s日" % (old_due, closed["start_day"], withdraw_day, withdraw_day, paused, new_deadline)
            changes["evidence_withdrawn"] = True
            changes["evidence_due_day"] = None
            changes["clock_paused"] = False
            changes["paused_days_remaining"] = None
            changes["tolling_intervals"] = intervals
            changes["deadline_day"] = new_deadline
            changes["response_day"] = withdraw_day
            changes["days_remaining"] = paused
            changes["overdue"] = paused < 0
            changes["deadline_basis"] = basis
            summary = "补件要求已撤回，原补件期限失效"
            calculation = {"basis": basis, "withdraw_day": withdraw_day, "previous_evidence_due_day": old_due, "released_days_remaining": paused, "deadline_day": new_deadline, "days_remaining": paused, "overdue": paused < 0, "tolling_intervals": intervals}
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            response_day = integer(data, "response_day", int(p["evidence_request_day"]))
            due = p.get("evidence_due_day")
            late_days = max(0, response_day - int(due)) if due is not None else 0
            paused = self._paused_remaining(p)
            intervals = [dict(item) for item in p.get("tolling_intervals", [])]
            closed = self._close_open_interval(intervals, response_day, "respond")
            new_deadline = response_day + paused
            basis = "补件回应：主期限恢复，新截止日=回应日第%s日+暂停时剩余%s天=第%s日；停表区间第%s-%s日保留" % (response_day, paused, new_deadline, closed["start_day"], response_day)
            if late_days:
                basis += "；回应日晚于补件期限第%s日，材料逾期%s天" % (int(due), late_days)
            changes["response_day"] = response_day
            changes["evidence_documents"] = docs
            changes["evidence_overdue"] = late_days > 0
            changes["late_days"] = late_days
            changes["clock_paused"] = False
            changes["paused_days_remaining"] = None
            changes["tolling_intervals"] = intervals
            changes["deadline_day"] = new_deadline
            changes["days_remaining"] = paused
            changes["overdue"] = paused < 0
            changes["deadline_basis"] = basis
            summary = "补件已回应" if not late_days else "补件已逾期回应，逾期%s天" % late_days
            calculation = {"basis": basis, "response_day": response_day, "evidence_due_day": due, "late_days": late_days, "evidence_overdue": late_days > 0, "released_days_remaining": paused, "deadline_day": new_deadline, "days_remaining": paused, "overdue": paused < 0, "tolling_intervals": intervals}
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action), calculation
