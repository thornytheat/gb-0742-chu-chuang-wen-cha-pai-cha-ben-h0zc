"""闭馆日结：按展柜连续小时核销多班交接。

核心约束：
- 同一展柜小时只核销一次：重叠小时用确定的交班规则归一班（早开班次优先），柜时不逐班相加。
- 各班原账不覆盖：小时保留每个班次自己的判级，仅对归属班取权威判级；分歧单列。
- 异常段按归属后的连续小时重新切段，跨交接也能连续；工单逐张落到具体小时，不丢单。
"""
from collections import Counter, defaultdict
from itertools import groupby

from app.errors import FieldError
from app.timebase import seconds, utc

HOUR = 3600
STATES = ("STABLE", "ABNORMAL", "MISSING")


def _run_view(archive):
    manifest, evidence = archive[0], archive[1]
    start, end = seconds(manifest["window"][0]), seconds(manifest["window"][1])
    grades = {(slot["cabinet"], slot["hour"]): slot for slot in evidence["grades"]}
    tickets = defaultdict(list)
    for ticket in evidence["audit"]["tickets"]:
        hour_epoch = seconds(ticket["at_utc"]) // HOUR * HOUR
        tickets[(ticket["cabinet"], hour_epoch)].append(ticket)
    return {
        "run_id": manifest["id"], "shift": manifest["shift"], "label": manifest["label"],
        "sample": manifest["sample"], "start": start, "end": end,
        "window": [manifest["window"][0], manifest["window"][1]],
        "cabinets": [cabinet["id"] for cabinet in manifest["cabinets"]],
        "result_sha256": manifest["result_sha256"], "grades": grades, "tickets": tickets,
    }


def _validate(runs):
    if len(runs) < 2:
        raise FieldError("SETTLEMENT_NEED_SHIFTS", "日结至少需要两个班次归卷", 422)
    seen = set()
    for run in runs:
        if run["run_id"] in seen:
            raise FieldError("SETTLEMENT_DUP_RUN", "同一归卷不能重复进入日结", 422)
        seen.add(run["run_id"])
        if run["start"] % HOUR or (run["end"] - run["start"]) % HOUR:
            raise FieldError("SETTLEMENT_BAD_WINDOW", "班次须按整点小时登记", 422)
    cabinets = {cabinet for run in runs for cabinet in run["cabinets"]}
    if not cabinets:
        raise FieldError("SETTLEMENT_NO_CABINET", "日结班次没有登记展柜", 422)
    day = runs[0]["start"] // 86400
    if any(run["start"] // 86400 != day for run in runs):
        raise FieldError("SETTLEMENT_CROSS_DAY", "日结只能合并同一闭馆日的班次", 422)
    return cabinets, day


def _build_hours(runs, cabinets, horizon_start, horizon_end):
    hours = []
    for cabinet in sorted(cabinets):
        for hour_epoch in range(horizon_start, horizon_end, HOUR):
            covering = [run for run in runs if run["start"] <= hour_epoch < run["end"]]
            if not covering:
                continue
            owner = covering[0]  # 早开班次优先：交接重叠小时归先开班
            slot = {
                "cabinet": cabinet, "hour": hour_epoch, "start": utc(hour_epoch),
                "end": utc(hour_epoch + HOUR), "owner_run": owner["run_id"],
                "owner_shift": owner["shift"], "overlap": len(covering) > 1,
                "covered_shifts": [run["shift"] for run in covering],
                "state": "MISSING", "conflict": False, "shift_states": [],
                "tickets": [], "episodes": [],
            }
            states = []
            for run in covering:
                grade = run["grades"].get((cabinet, hour_epoch))
                state = grade["state"] if grade else "MISSING"
                states.append(state)
                slot["shift_states"].append({
                    "run_id": run["run_id"], "shift": run["shift"], "state": state,
                    "code": grade.get("code") if grade else None,
                    "upper": grade["upper"] if grade else None,
                    "lower": grade["lower"] if grade else None,
                })
                for ticket in run["tickets"].get((cabinet, hour_epoch), []):
                    slot["tickets"].append({
                        **ticket, "source_run": run["run_id"], "source_shift": run["shift"],
                        "in_overlap": len(covering) > 1, "owner_shift": owner["shift"],
                    })
            slot["conflict"] = len(set(states)) > 1
            slot["state"] = next(item["state"] for item in slot["shift_states"]
                                 if item["run_id"] == owner["run_id"])
            hours.append(slot)
    return hours


def _segment(cabinets, hours, sustained_hours):
    """按归属后的连续异常小时重新切段，段号每柜独立；同时回填小时的段引用。"""
    index = {(slot["cabinet"], slot["hour"]): slot for slot in hours}
    episodes = []
    for cabinet in sorted(cabinets):
        abnormal = sorted((slot for slot in hours
                           if slot["cabinet"] == cabinet and slot["state"] == "ABNORMAL"),
                          key=lambda slot: slot["hour"])
        groups = groupby(enumerate(abnormal), key=lambda item: item[1]["hour"] - item[0] * HOUR)
        for serial, (_, group) in enumerate(groups, 1):
            members = [slot for _, slot in group]
            episode_id = f"{cabinet}-D{serial:02d}"
            for slot in members:
                slot["episodes"].append(episode_id)
            episodes.append({
                "id": episode_id, "cabinet": cabinet,
                "kind": "SUSTAINED" if len(members) >= sustained_hours else "TRANSIENT",
                "start": members[0]["start"], "end": members[-1]["end"],
                "hours": len(members),
                "crosses_handover": len({slot["owner_run"] for slot in members}) > 1,
                "owner_shifts": sorted({slot["owner_shift"] for slot in members}),
            })
    return episodes


def _summary(runs, cabinets, hours, horizon_start, horizon_end):
    tally = Counter(slot["state"] for slot in hours)
    overlap = [slot for slot in hours if slot["overlap"]]
    expected = ((horizon_end - horizon_start) // HOUR) * len(cabinets)
    tickets = [ticket for slot in hours for ticket in slot["tickets"]]
    shift_hour_sum = {run["shift"]: sum(
        1 for cabinet in cabinets for hour_epoch in range(run["start"], run["end"], HOUR))
        for run in runs}
    return {
        "cabinet_count": len(cabinets),
        "tally": {"expected": expected, **{state.lower(): tally[state] for state in STATES}},
        "overlap": {
            "hours": len({(slot["cabinet"], slot["hour"]) for slot in overlap}),
            "conflicts": sum(1 for slot in overlap if slot["conflict"]),
            "shift_sum": sum(shift_hour_sum.values()),
            "unique": len(hours),
        },
        "tickets": tickets,
        "ticket_count": len(tickets),
        "ticket_codes": dict(Counter(ticket["code"] for ticket in tickets)),
        "shifts_hour_sum": shift_hour_sum,
    }


def settle(archives, sustained_hours):
    """输入各班归卷 (manifest, evidence, source)，输出可序列化的日结账。"""
    runs = sorted((_run_view(archive) for archive in archives),
                  key=lambda run: (run["start"], run["run_id"]))
    cabinets, day = _validate(runs)
    horizon_start = min(run["start"] for run in runs)
    horizon_end = max(run["end"] for run in runs)
    hours = _build_hours(runs, cabinets, horizon_start, horizon_end)
    episodes = _segment(cabinets, hours, sustained_hours)
    summary = _summary(runs, cabinets, hours, horizon_start, horizon_end)
    shifts = [{
        "run_id": run["run_id"], "shift": run["shift"], "label": run["label"],
        "sample": run["sample"], "window": run["window"], "result_sha256": run["result_sha256"],
        "order": index,
    } for index, run in enumerate(runs)]
    return {
        "day": utc(day * 86400)[:10], "day_epoch": day * 86400,
        "window": [utc(horizon_start), utc(horizon_end)],
        "sustained_hours": sustained_hours, "shifts": shifts,
        "hours": hours, "episodes": episodes, **summary,
    }
