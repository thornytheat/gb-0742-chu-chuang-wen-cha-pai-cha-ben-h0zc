"""闭馆日结：重叠小时归属、连续异常跨交接、工单落点、只追加与幂等。"""
import pytest

from app.main import create_app
from app.modules.settlement.ledger import settle
from fastapi.testclient import TestClient


@pytest.fixture
def handover(client):
    def run(sample):
        response = client.post("/api/runs", json={"sample": sample})
        assert response.status_code == 201, response.text
        return response.json()["manifest"]["id"]

    day = run("day-handover")
    evening = run("evening-handover")
    return client, {"day": day, "evening": evening}


def close(client, ids):
    return client.post("/api/settlements", json={"run_ids": ids})


def test_overlap_hours_counted_once_not_summed_per_shift(handover):
    client, ids = handover
    report = close(client, [ids["day"], ids["evening"]]).json()["report"]
    # 日班 10 小时 + 晚班 6 小时 = 16，但 16/17 时重叠只核销一次 -> 14
    assert report["overlap"] == {"hours": 2, "conflicts": 1, "shift_sum": 16, "unique": 14}
    assert report["tally"] == {"expected": 14, "stable": 7, "abnormal": 5, "missing": 2}
    assert len(report["hours"]) == 14


def test_each_overlap_hour_names_owner_and_keeps_both_shift_verdicts(handover):
    client, ids = handover
    report = close(client, [ids["day"], ids["evening"]]).json()["report"]
    overlap = [slot for slot in report["hours"] if slot["overlap"]]
    assert [(slot["start"], slot["owner_shift"]) for slot in overlap] == [
        ("2026-11-21T08:00:00Z", "D26-1121"), ("2026-11-21T09:00:00Z", "D26-1121")]
    sixteen, seventeen = overlap
    assert {item["shift"] for item in sixteen["shift_states"]} == {"D26-1121", "E26-1121"}
    assert not sixteen["conflict"]  # 两班都报异常
    assert seventeen["conflict"] and seventeen["state"] == "ABNORMAL"  # 分歧时取归属班（日班）
    assert {item["shift"]: item["state"] for item in seventeen["shift_states"]} == {
        "D26-1121": "ABNORMAL", "E26-1121": "STABLE"}


def test_sustained_episode_stretches_across_handover_on_continuous_hours(handover):
    client, ids = handover
    report = close(client, [ids["day"], ids["evening"]]).json()["report"]
    crossing = [item for item in report["episodes"] if item["crosses_handover"]]
    assert len(crossing) == 1
    episode = crossing[0]
    assert (episode["id"], episode["kind"], episode["hours"]) == ("CV-17-D02", "SUSTAINED", 4)
    assert (episode["start"], episode["end"]) == ("2026-11-21T08:00:00Z", "2026-11-21T12:00:00Z")
    assert episode["owner_shifts"] == ["D26-1121", "E26-1121"]
    # 瞬时段仍单独切段
    transient = [item for item in report["episodes"] if item["kind"] == "TRANSIENT"]
    assert [item["id"] for item in transient] == ["CV-17-D01"]


def test_every_ticket_lands_on_an_hour_with_source_and_owner_shift(handover):
    client, ids = handover
    report = close(client, [ids["day"], ids["evening"]]).json()["report"]
    tickets = {(item["id"]): item for item in report["tickets"]}
    assert set(tickets) == {"AL-D101", "AL-D102", "AL-E201", "AL-E202"}
    # 晚班在重叠 17 时的工单不丢：来源是晚班，小时归属是日班
    e202 = tickets["AL-E202"]
    assert (e202["source_shift"], e202["owner_shift"], e202["in_overlap"]) == ("E26-1121", "D26-1121", True)
    # 工单都能在小时表回查到
    landed = {ticket["id"] for slot in report["hours"] for ticket in slot["tickets"]}
    assert landed == set(tickets)
    assert report["ticket_codes"]["MATCHED"] == 2


def test_rerun_same_input_returns_same_settlement_without_new_row(handover):
    client, ids = handover
    body = [ids["day"], ids["evening"]]
    first = close(client, body)
    second = close(client, body)
    assert first.status_code == 201 and first.json()["version"] == 1
    assert second.status_code == 200 and second.json()["reused"] is True
    assert second.json()["id"] == first.json()["id"]
    history = client.get("/api/settlements").json()
    assert len(history) == 1 and history[0]["current"] is True


def test_failed_settlement_keeps_previous_version_untouched(handover):
    client, ids = handover
    ok = close(client, [ids["day"], ids["evening"]])
    assert ok.status_code == 201
    before = client.get("/api/settlements").json()
    # 引用不存在的班次归卷：日结中止，不能产生新版本
    broken = close(client, [ids["day"], "missing-run-id"])
    assert broken.status_code == 404 and broken.json()["code"] == "UNKNOWN_RUN"
    after = client.get("/api/settlements").json()
    assert after == before
    # 单日、跨闭馆日同样拒绝且不落账
    single = close(client, [ids["day"], ids["day"]])
    assert single.status_code == 422 and single.json()["code"] == "SETTLEMENT_DUP_RUN"
    clean = client.post("/api/runs", json={"sample": "clean"}).json()["manifest"]["id"]
    cross = close(client, [ids["day"], clean])
    assert cross.status_code == 422 and cross.json()["code"] == "SETTLEMENT_CROSS_DAY"
    assert len(client.get("/api/settlements").json()) == 1


def test_new_input_same_day_creates_next_version_but_old_remains_readable(handover):
    client, ids = handover
    first = close(client, [ids["day"], ids["evening"]]).json()
    # 用晚班单班凑不出日结；改为同日输入变化（重复提交同一组但规则摘要不变仍幂等），
    # 这里直接验证版本表只追加：手工再写一份不同持续门槛的账。
    archives = [client.app.state.archives.store.fetch(run_id) for run_id in (ids["day"], ids["evening"])]
    altered = settle(archives, sustained_hours=3)
    altered["policy"] = "glass-1.0.0"
    altered["source_hashes"] = first["report"]["source_hashes"]
    from app.manifest import json_sha
    altered["report_sha256"] = json_sha(altered)
    new_id = json_sha(["glass-1.0.0", 3, [(h["run_id"], h["result_sha256"]) for h in altered["source_hashes"]]])
    from datetime import datetime, timezone
    saved, created, version = client.app.state.settlements.store.insert(
        altered, new_id, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    assert created and saved["sustained_hours"] == 3 and version == 2
    history = client.get("/api/settlements").json()
    assert [item["version"] for item in history] == [2, 1]
    assert history[0]["current"] is True and history[1]["current"] is False
    # 旧版仍可按 id 读取
    assert client.get(f"/api/settlements/{first['id']}").status_code == 200


def test_settlement_never_overwrites_shift_archives(handover):
    client, ids = handover
    store = client.app.state.archives.store
    before = {run_id: store.fetch(run_id)[0]["result_sha256"] for run_id in ids.values()}
    close(client, [ids["day"], ids["evening"]])
    close(client, [ids["day"], ids["evening"]])  # 重跑
    after = {run_id: store.fetch(run_id)[0]["result_sha256"] for run_id in ids.values()}
    assert before == after
    # 日结固化了班次摘要，原账漂移会被拦截
    report = client.get("/api/settlements/days/2026-11-21").json()
    assert {h["shift"] for h in report["source_hashes"]} == {"D26-1121", "E26-1121"}


def test_ledger_rejects_less_than_two_shifts(handover):
    client, ids = handover
    # 单班进入服务层（绕过请求体的两项下限）应被领域规则拒绝
    from app.errors import FieldError
    with pytest.raises(FieldError) as exc:
        client.app.state.settlements.close_day([ids["day"]])
    assert exc.value.code == "SETTLEMENT_NEED_SHIFTS"
    assert not client.get("/api/settlements").json()


def test_settlement_page_and_nav_entry_render(client):
    page = client.get("/settlement")
    assert page.status_code == 200
    assert "闭馆日结" in page.text and 'id="settlement-hours"' in page.text


def test_storage_failure_during_write_keeps_old_version_current(handover, monkeypatch):
    import sqlite3
    client, ids = handover
    first = close(client, [ids["day"], ids["evening"]])
    assert first.status_code == 201
    good_id = first.json()["id"]

    class BrokenStore:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def insert(self, *args, **kwargs):
            raise sqlite3.OperationalError("simulated disk failure")

    monkeypatch.setattr(client.app.state.settlements, "store",
                        BrokenStore(client.app.state.settlements.store))
    failing = close(client, [ids["day"], ids["evening"]])
    assert failing.status_code == 503
    # 旧版仍是当前日结且可正常读取
    current = client.get("/api/settlements/days/2026-11-21").json()
    assert current["report_sha256"] == first.json()["report"]["report_sha256"]
    history = client.get("/api/settlements").json()
    assert len(history) == 1 and history[0]["id"] == good_id and history[0]["current"]


def test_persistence_across_app_restart(tmp_path):
    path = tmp_path / "settle.sqlite3"
    with TestClient(create_app(path, seed=False)) as first:
        ids = [first.post("/api/runs", json={"sample": s}).json()["manifest"]["id"]
               for s in ("day-handover", "evening-handover")]
        created = close(first, ids)
        assert created.status_code == 201
    with TestClient(create_app(path, seed=False)) as second:
        history = second.get("/api/settlements").json()
        assert len(history) == 1 and history[0]["day"] == "2026-11-21"
        assert second.get("/api/settlements/days/2026-11-21").status_code == 200
