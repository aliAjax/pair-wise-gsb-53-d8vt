"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Conflict, ValidationError, boolean, choice, integer, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {
    'submit': {'legal_rep', 'case_officer'},
    'request_evidence': {'case_officer'},
    'confirm_delivery': {'case_officer', 'intake_officer'},
    'update_delivery': {'case_officer', 'supervisor'},
    'respond': {'legal_rep'},
    'withdraw_evidence': {'supervisor'},
    'return_evidence': {'case_officer', 'intake_officer'},
    'decide': {'case_officer', 'supervisor'},
    'appeal': {'legal_rep'},
    'close': {'supervisor'},
}
TRANSITIONS = {
    'submit': {'draft': 'submitted'},
    'request_evidence': {'submitted': 'evidence_requested'},
    'confirm_delivery': {'evidence_requested': 'evidence_requested'},
    'update_delivery': {'evidence_requested': 'evidence_requested'},
    'respond': {'evidence_requested': 'response_received'},
    'withdraw_evidence': {'evidence_requested': 'submitted'},
    'return_evidence': {'evidence_requested': 'submitted'},
    'decide': {'submitted': 'decided', 'response_received': 'decided'},
    'appeal': {'decided': 'appealed'},
    'close': {'decided': 'closed', 'appealed': 'closed'},
}
# 送达方式对应的额外在途天数：电子送达即时到达，快递次日，邮寄三日
DELIVERY_EXTRA_DAYS = {'electronic': 0, 'courier': 1, 'mail': 3}
EVIDENCE_REASON = 'evidence_request'


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
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        p["tolling_intervals"] = []
        p["late_response"] = False
        p["evidence_request_day"] = None
        p["evidence_due_day"] = None
        p["evidence_delivery_confirmed"] = False
        p["delivery_method"] = None
        p["delivery_day"] = None
        self._refresh(p)
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

    @staticmethod
    def _open_interval(intervals: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for interval in reversed(intervals):
            if interval.get("reason") == EVIDENCE_REASON and not interval.get("annulled") and interval.get("end_day") is None:
                return interval
        return None

    def _refresh(self, p: Dict[str, Any], recalculation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """按停表区间重算有效期限，并把计算依据写回记录。"""
        base = int(p["deadline_day"])
        current = int(p["response_day"])
        tolled = 0
        open_start = None
        for interval in p.get("tolling_intervals") or []:
            if interval.get("annulled"):
                continue
            end_day = interval.get("end_day")
            if end_day is None:
                open_start = int(interval["start_day"])
            else:
                tolled += max(0, int(end_day) - int(interval["start_day"]))
        effective = base + tolled
        clock_day = current if open_start is None else open_start  # 停表期间剩余天数冻结在停表当天
        days_remaining = effective - clock_day
        overdue = days_remaining < 0 or bool(p.get("late_response"))
        basis: Dict[str, Any] = {
            "base_deadline_day": base,
            "tolled_days": tolled,
            "clock_stopped": open_start is not None,
            "open_tolling_start_day": open_start,
            "effective_deadline_day": effective,
            "current_day": current,
            "days_remaining": days_remaining,
            "overdue": overdue,
            "late_response": bool(p.get("late_response")),
        }
        if p.get("evidence_due_day") is not None:
            basis["evidence_due_day"] = int(p["evidence_due_day"])
            basis["evidence_days_remaining"] = int(p["evidence_due_day"]) - current
            basis["evidence_delivery_confirmed"] = bool(p.get("evidence_delivery_confirmed"))
            if p.get("delivery_method"):
                basis["delivery_method"] = p["delivery_method"]
                basis["delivery_day"] = p.get("delivery_day")
                basis["delivery_extra_days"] = DELIVERY_EXTRA_DAYS.get(p["delivery_method"], 0)
        if recalculation:
            basis["recalculation"] = recalculation
        p["days_remaining"] = days_remaining
        p["overdue"] = overdue
        p["deadline_basis"] = basis
        return basis

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        p.setdefault("late_response", False)
        intervals = [dict(interval) for interval in p.get("tolling_intervals") or []]
        changes: Dict[str, Any] = {}
        recalculation: Dict[str, Any] = {"trigger": action}
        summary = ""
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
            interval = {
                "start_day": request_day,
                "end_day": None,
                "reason": EVIDENCE_REASON,
                "delivery_method": None,
                "delivery_day": None,
                "annulled": False,
            }
            changes["tolling_intervals"] = intervals + [interval]
            changes["evidence_request_day"] = request_day
            changes["allowed_days"] = allowed_days
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            changes["evidence_delivery_confirmed"] = False
            changes["delivery_method"] = None
            changes["delivery_day"] = None
            changes["response_day"] = max(int(p["response_day"]), request_day)
            recalculation.update({"note": "补件发出，剩余天数暂停计算", "tolling_start_day": request_day, "provisional_evidence_due_day": changes["evidence_due_day"]})
            summary = "补件要求已发出，剩余天数暂停计算"
        elif action == "confirm_delivery":
            interval = self._open_interval(intervals)
            if interval is None or p.get("evidence_request_day") is None:
                raise ValidationError("当前没有进行中的补件要求")
            if p.get("evidence_delivery_confirmed"):
                raise ValidationError("送达已确认，如需调整请变更送达方式")
            delivery_day = integer(data, "delivery_day", int(p["evidence_request_day"]))
            method = choice(data, "delivery_method", list(DELIVERY_EXTRA_DAYS))
            previous_due = p.get("evidence_due_day")
            due = delivery_day + int(p["allowed_days"]) + DELIVERY_EXTRA_DAYS[method]
            interval["delivery_method"] = method
            interval["delivery_day"] = delivery_day
            changes["tolling_intervals"] = intervals
            changes["delivery_method"] = method
            changes["delivery_day"] = delivery_day
            changes["evidence_due_day"] = due
            changes["evidence_delivery_confirmed"] = True
            changes["response_day"] = max(int(p["response_day"]), delivery_day)
            recalculation.update({"note": "送达确认后按送达方式重算补件期限", "previous_evidence_due_day": previous_due, "evidence_due_day": due, "delivery_method": method, "delivery_day": delivery_day, "delivery_extra_days": DELIVERY_EXTRA_DAYS[method]})
            summary = "送达已确认，补件期限已重算"
        elif action == "update_delivery":
            interval = self._open_interval(intervals)
            if interval is None or not p.get("evidence_delivery_confirmed"):
                raise ValidationError("尚未确认送达，无法变更送达方式")
            method = choice(data, "delivery_method", list(DELIVERY_EXTRA_DAYS))
            delivery_day = int(p["delivery_day"])
            if "delivery_day" in data:
                delivery_day = integer(data, "delivery_day", int(p["evidence_request_day"]))
            previous_due = p.get("evidence_due_day")
            previous_method = p.get("delivery_method")
            due = delivery_day + int(p["allowed_days"]) + DELIVERY_EXTRA_DAYS[method]
            interval["delivery_method"] = method
            interval["delivery_day"] = delivery_day
            changes["tolling_intervals"] = intervals
            changes["delivery_method"] = method
            changes["delivery_day"] = delivery_day
            changes["evidence_due_day"] = due
            recalculation.update({"note": "送达方式变更，旧期限立即失效并重算", "previous_delivery_method": previous_method, "previous_evidence_due_day": previous_due, "evidence_due_day": due, "delivery_method": method, "delivery_day": delivery_day, "delivery_extra_days": DELIVERY_EXTRA_DAYS[method]})
            summary = "送达方式已变更，补件期限已重算"
        elif action == "respond":
            interval = self._open_interval(intervals)
            response_day = integer(data, "response_day", 0)
            if p.get("evidence_request_day") is not None and response_day < int(p["evidence_request_day"]):
                raise ValidationError("补件回应日期不能早于补件发出日期")
            docs = text_list(data, "documents", 1)
            due = p.get("evidence_due_day")
            late = due is not None and response_day > int(due)
            if interval is not None:
                interval["end_day"] = response_day
                changes["tolling_intervals"] = intervals
            changes["response_day"] = response_day
            changes["evidence_documents"] = docs
            changes["late_response"] = bool(p.get("late_response")) or late
            recalculation.update({"note": "补件回应，停表结束", "tolling_end_day": response_day, "late": late, "evidence_due_day": due})
            summary = "补件已回应（逾期）" if late else "补件已回应"
        elif action in ("withdraw_evidence", "return_evidence"):
            interval = self._open_interval(intervals)
            if interval is None:
                raise ValidationError("当前没有进行中的补件要求")
            day_key = "withdraw_day" if action == "withdraw_evidence" else "return_day"
            day = integer(data, day_key, int(p["evidence_request_day"]))
            interval["end_day"] = day
            interval["annulled"] = True
            interval["annulled_by"] = action
            changes["tolling_intervals"] = intervals
            previous_due = p.get("evidence_due_day")
            changes["evidence_request_day"] = None
            changes["evidence_due_day"] = None
            changes["evidence_delivery_confirmed"] = False
            changes["delivery_method"] = None
            changes["delivery_day"] = None
            changes["response_day"] = max(int(p["response_day"]), day)
            note = "补件要求已撤销，恢复原期限" if action == "withdraw_evidence" else "补件通知被退回，恢复原期限"
            recalculation.update({"note": note, "annulled_interval": dict(interval), "previous_evidence_due_day": previous_due, "restored_deadline_day": int(p["deadline_day"])})
            summary = note
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
        self._refresh(p, recalculation)
        return new_state, p, summary or ("已执行%s" % action)

    def backfill_tolling(self, record: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """旧数据升级：按当前案件状态补齐停表记录，返回(新payload, 计算依据)。"""
        p = dict(record["payload"])
        if "tolling_intervals" in p and "deadline_basis" in p:
            return None
        p.setdefault("deadline_day", int(p.get("received_day", 0)) + int(p.get("deadline_days", 0)))
        p.setdefault("response_day", 0)
        intervals = [dict(interval) for interval in p.get("tolling_intervals") or []]
        request_day = p.get("evidence_request_day")
        if request_day is not None and not any(interval.get("reason") == EVIDENCE_REASON for interval in intervals):
            end_day = None
            if record["state"] != "evidence_requested":
                current = int(p.get("response_day", request_day))
                if current >= int(request_day):
                    end_day = current
            intervals.append({
                "start_day": int(request_day),
                "end_day": end_day,
                "reason": EVIDENCE_REASON,
                "delivery_method": p.get("delivery_method"),
                "delivery_day": p.get("delivery_day"),
                "annulled": False,
                "migrated": True,
            })
        p["tolling_intervals"] = intervals
        p.setdefault("evidence_request_day", None)
        p.setdefault("evidence_due_day", None)
        p.setdefault("evidence_delivery_confirmed", False)
        p.setdefault("delivery_method", None)
        p.setdefault("delivery_day", None)
        p.setdefault("late_response", False)
        due = p.get("evidence_due_day")
        if due is not None and record["state"] not in {"draft", "submitted", "evidence_requested"} and int(p.get("response_day", 0)) > int(due):
            p["late_response"] = True
        basis = self._refresh(p, {"trigger": "migration", "note": "旧数据升级：按当前案件状态补齐停表记录"})
        return p, basis
