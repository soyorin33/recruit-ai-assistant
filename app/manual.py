"""人工兜底：系统出问题时，HR 在网页上手动录入 / 修改，以及处理「待人工处理」队列。

- 手动录入 / 编辑不经过 AI，但仍然走同一套规则：去重（手机号 + 岗位）、状态机校验、变更流水、腾讯文档同步。
  有风险时（阶段回退、改岗位、同一人投新岗位……）先返回提示，HR 点「仍然保存」才写入。
- 手动编辑是 HR 的明确意图：允许改渠道（自动录入时渠道以首次投递为准，这里不受限），允许清空字段。
- 「待人工处理」：消息处理异常、没识别出候选人、简历 / 截图解析失败、表格同步重试仍失败，都会进入这个队列，
  HR 可以重试、手动录入或标记已处理，保证不丢数据。
"""
from __future__ import annotations

from datetime import datetime

from . import bus
from .db import db
from .domain import (EDITABLE_FIELDS, STAGES, STATUS_ACTIVE, STATUS_HIRED, STATUSES, normalize_channel,
                     normalize_phone, normalize_position)

MANUAL_RAW = "（网页手动录入）"
KIND_LABELS = {"message": "消息处理异常", "unrecognized": "没识别出候选人", "resume": "简历解析失败",
               "image": "截图识别失败", "wecom": "企微消息处理失败", "sync": "表格同步失败"}
RETRYABLE = {"message", "unrecognized", "wecom", "sync"}


class ManualError(ValueError):
    pass


def clean_fields(raw: dict, creating: bool) -> dict:
    """校验并归一化表单。空字符串表示清空该字段（仅编辑时有效）。"""
    out: dict = {}
    for k in EDITABLE_FIELDS:
        if k not in raw or raw[k] is None:
            continue
        v = str(raw[k]).strip()
        if not v:
            if not creating and k != "name":
                out[k] = None
            continue
        if k == "phone":
            v = normalize_phone(v)
        elif k == "position":
            v = normalize_position(v) or v
        elif k == "channel":
            v = normalize_channel(v) or v
        elif k == "stage" and v not in STAGES:
            raise ManualError(f"阶段只能是：{'、'.join(STAGES)}")
        elif k == "status" and v not in STATUSES:
            raise ManualError(f"状态只能是：{'、'.join(STATUSES)}")
        elif k == "interview_time":
            v = v.replace("T", " ")[:16]  # 兼容浏览器 datetime-local 的「2026-11-25T15:00」
            try:
                datetime.strptime(v, "%Y-%m-%d %H:%M")
            except ValueError as e:
                raise ManualError("面试时间格式应为 YYYY-MM-DD HH:MM") from e
        elif k == "match_score":
            try:
                v = max(0, min(100, int(float(v))))
            except ValueError as e:
                raise ManualError("匹配度应为 0～100 的数字") from e
        out[k] = v
    if creating and not out.get("name"):
        raise ManualError("姓名必填")
    if out.get("stage") == "已入职" or out.get("status") == STATUS_HIRED:
        out["stage"], out["status"] = "已入职", STATUS_HIRED
    return out


def save(raw: dict, *, operator: str, candidate_id: int | None = None, force: bool = False,
         failure_id: int | None = None) -> dict:
    """手动新建（candidate_id 为空）或编辑。返回：
    {"ok": True, "candidate": ...} 已保存；
    {"ok": False, "needs_confirm": True, "reasons": [...]} 有风险，需 HR 确认后带 force=True 再提交；
    {"ok": False, "duplicate": {...}} 新建时发现已有同一人同岗位的记录，应改为编辑它。
    """
    from .pipeline import WRITE_LOCK, apply_plan, plan_change, public_candidate
    fields = clean_fields(raw, creating=candidate_id is None)
    with WRITE_LOCK:
        if candidate_id is None:
            plan = plan_change({k: v for k, v in fields.items() if v is not None})
            if plan["op"] != "create":  # 去重命中：已经有这个人（同手机号同岗位 / 同名同岗位）
                existing = db.get_candidate(plan["candidate_id"])
                return {"ok": False, "duplicate": public_candidate(existing),
                        "message": f"已存在「{existing['name']}·{existing.get('position') or '岗位未填'}」"
                                   f"（编号 {existing['id']}），请改为编辑这条记录"}
            plan["fields"].setdefault("stage", "简历筛选")
            plan["fields"].setdefault("status", STATUS_ACTIVE)
        else:
            existing = db.get_candidate(candidate_id)
            if not existing:
                raise ManualError(f"编号 {candidate_id} 的候选人不存在")
            # 借用流水线的规则得到风险提示（阶段回退、改岗位、重新激活……），但字段以 HR 填写的为准
            check = plan_change({k: v for k, v in {**fields, "name": existing["name"],
                                                     "candidate_id": candidate_id}.items() if v is not None})
            new = dict(fields)
            if "stage" in new and new["stage"] != existing["stage"] and "status" not in new:
                new["status"] = STATUS_ACTIVE if new["stage"] != "已入职" else STATUS_HIRED
            changes = {k: [existing.get(k), v] for k, v in new.items() if (existing.get(k) or None) != (v or None)}
            plan = {"op": "update" if changes else "noop", "name": existing["name"], "candidate_id": candidate_id,
                    "fields": {k: v[1] for k, v in changes.items()}, "changes": changes,
                    "reasons": [r for r in check["reasons"] if not r.startswith("编号")], "notes": []}
        if plan["reasons"] and not force and plan["op"] != "noop":
            return {"ok": False, "needs_confirm": True, "reasons": plan["reasons"]}
        cand = apply_plan(plan, operator=operator, raw_text=MANUAL_RAW, engine="manual", source="manual")
    if failure_id:
        db.resolve_failure(failure_id, operator, f"已手动录入：{cand['name']}（编号 {cand['id']}）")
        bus.publish("failure", {"id": failure_id})
    return {"ok": True, "op": plan["op"], "candidate": public_candidate(cand), "changes": plan["changes"]}


# ---------------- 待人工处理 ----------------
def record(kind: str, *, raw_text: str = "", error: str = "", source: str = "", operator: str = "",
           ref_id: int | None = None) -> int:
    try:
        fid = db.add_failure(kind, raw_text=raw_text, error=str(error), source=source, operator=operator, ref_id=ref_id)
    except Exception:  # noqa: BLE001  记录失败本身不能再让业务出错
        return 0
    bus.publish("failure", {"id": fid})
    return fid


def list_open() -> list[dict]:
    return [{**f, "kind_label": KIND_LABELS.get(f["kind"], f["kind"]), "retryable": f["kind"] in RETRYABLE}
            for f in db.list_failures("open")]


def retry(fid: int, operator: str) -> dict:
    f = db.get_failure(fid)
    if not f or f["state"] != "open":
        raise ManualError("这条记录已处理过了")
    if f["kind"] not in RETRYABLE:
        raise ManualError("文件需要重新发送，或直接手动录入")
    if f["kind"] == "sync":
        from . import sync
        if not sync.retry(f["ref_id"]):
            raise ManualError("同步目标未启用，无法重试")
        db.resolve_failure(fid, operator, "已重新加入同步队列")
        result = {"ok": True, "text": "已重新加入同步队列"}
    else:
        from .pipeline import handle_message
        last = (db.one("SELECT MAX(id) AS m FROM failures") or {}).get("m") or 0
        msg = handle_message(f["raw_text"], operator=f["operator"] or operator, source=f["source"] or "web")
        again = [r["id"] for r in db.q("SELECT id FROM failures WHERE id>? AND state='open'", (last,))]
        if msg["payload"].get("failure_id") or again:
            for nid in again:  # 重试又失败：新产生的那条并入原记录，不重复出现在队列里
                db.resolve_failure(nid, operator, f"重试失败，并入 #{fid}")
            result = {"ok": False, "text": "重试仍然没能自动处理，请手动录入", "reply": msg["text"]}
        else:
            db.resolve_failure(fid, operator, "重试成功")
            result = {"ok": True, "text": msg["text"]}
    bus.publish("failure", {"id": fid})
    return result


def resolve(fid: int, operator: str, note: str = "") -> None:
    db.resolve_failure(fid, operator, note or "已人工处理")
    bus.publish("failure", {"id": fid})
