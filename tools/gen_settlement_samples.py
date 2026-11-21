"""生成闭馆日结样例：同一闭馆日两班，交接重叠两个小时。

脚本只用于生成仓内样例，运行时不参与排查链路。
输出：
  sample_data/day-handover.csv / day-handover.json      日班 08:00-18:00
  sample_data/evening-handover.csv / evening-handover.json 晚班 16:00-22:00
两班均监测 CV-17，重叠小时为 16:00 与 17:00（馆内时间 UTC+08）。
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sample_data"
DAY = "2026-11-21"
CABINET = {
    "id": "CV-17", "label": "北廊 · 青瓷橱窗",
    "upper_mm": 1550, "lower_mm": 550, "temp_limit": "2.0", "rh_limit": "8.0",
}

# 每小时一条上层、一条下层；温度为 (上层, 下层)。
# 判级：层间距 1.000 m，阈值 2.0 °C/m，σ 各 0.10，半宽约 0.277，
# 温差达到 3 °C 时整区间严格高于阈值 -> 异常；温差 1.5 °C 时落在安全区间 -> 稳定。
PLAN = {
    "day-handover": {
        "label": "交接日班样例", "shift": "D26-1121",
        "start": f"{DAY}T08:00:00+08:00", "end": f"{DAY}T18:00:00+08:00",
        "csv": "day-handover.csv",
        "temps": {
            8: (21.5, 20.0), 9: (21.5, 20.0), 10: (24.5, 20.0),   # 10 时单小时异常（瞬时）
            11: (21.5, 20.0), 12: (21.5, 20.0), 13: (21.5, 20.0),
            14: (21.5, 20.0), 15: (21.5, 20.0),
            16: (24.5, 20.0), 17: (24.5, 20.0),                    # 重叠两小时连续异常（日班口径）
        },
        "tickets": [
            {"id": "AL-D101", "cabinet": "CV-17", "at": f"{DAY}T10:20:00+08:00"},  # 落在 10 时瞬时段
            {"id": "AL-D102", "cabinet": "CV-17", "at": f"{DAY}T16:40:00+08:00"},  # 落在重叠 16 时
        ],
        "probes": {"upper": "0101", "lower": "0102"},
    },
    "evening-handover": {
        "label": "交接晚班样例", "shift": "E26-1121",
        "start": f"{DAY}T16:00:00+08:00", "end": f"{DAY}T22:00:00+08:00",
        "csv": "evening-handover.csv",
        "temps": {
            16: (24.5, 20.0),                                      # 重叠：与日班同判级异常
            17: (21.5, 20.0),                                      # 重叠：晚班报稳定，与日班异常不一致（交接差账）
            18: (24.5, 20.0), 19: (24.5, 20.0),                    # 18-19 时持续异常
            20: (21.5, 20.0),                                      # 20 时缺下层 -> 缺证
            # 21 时整小时无读数 -> 缺证
        },
        "tickets": [
            {"id": "AL-E201", "cabinet": "CV-17", "at": f"{DAY}T18:35:00+08:00"},  # 18 时，核销持续段
            {"id": "AL-E202", "cabinet": "CV-17", "at": f"{DAY}T17:25:00+08:00"},  # 重叠 17 时，晚班工单
        ],
        "probes": {"upper": "0201", "lower": "0202"},
    },
}

HEADER = ("probe", "observed", "received", "temp", "temp_unit", "temp_sigma",
          "rh", "rh_unit", "rh_sigma", "memo")


def write_sample(sample, spec):
    rows = []
    for hour, (up_temp, low_temp) in sorted(spec["temps"].items()):
        observed_up = f"{DAY}T{hour:02d}:10:00+08:00"
        observed_low = f"{DAY}T{hour:02d}:12:00+08:00"
        rows.append([spec["probes"]["upper"], observed_up, observed_up,
                     str(up_temp), "C", ".10", "50", "PCT", ".20", ""])
        if sample == "evening-handover" and hour == 20:
            continue  # 20 时仅上层有读数，构造缺证柜时
        rows.append([spec["probes"]["lower"], observed_low, observed_low,
                     str(low_temp), "C", ".10", "50", "PCT", ".20", ""])
    with (OUT / spec["csv"]).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADER)
        writer.writerows(rows)
    contract = {
        "sample": sample, "label": spec["label"], "shift": spec["shift"],
        "start": spec["start"], "end": spec["end"], "cabinet_hours": [CABINET],
        "tolerance_seconds": 240, "late_seconds": 900, "sustained_hours": 2,
        "deployments": [
            {"probe": spec["probes"]["upper"], "cabinet": CABINET["id"], "level": "upper",
             "start": spec["start"], "end": spec["end"]},
            {"probe": spec["probes"]["lower"], "cabinet": CABINET["id"], "level": "lower",
             "start": spec["start"], "end": spec["end"]},
        ],
        "tickets": spec["tickets"], "csv": spec["csv"], "records": len(rows),
    }
    (OUT / f"{sample}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(sample, "rows:", len(rows))


def main():
    for sample, spec in PLAN.items():
        write_sample(sample, spec)


if __name__ == "__main__":
    main()
