"""闭馆日结服务。

班次原账（shift_archives）全程只读：服务只按归卷号读取并核对结果摘要，
日结把归卷号与摘要固化在自己的版本里。计算阶段不产生任何写入，
仅在全部账算平后调用只追加存储；同输入重跑返回旧版、不新增日结。
"""
from datetime import datetime, timezone

from app.config import POLICY
from app.errors import FieldError
from app.manifest import json_sha
from app.modules.settlement.ledger import settle
from app.modules.settlement.store import SettlementStore


class SettlementService:
    def __init__(self, store: SettlementStore, archive_service):
        self.store = store
        self.archives = archive_service

    def _collect(self, run_ids):
        if not run_ids:
            raise FieldError("SETTLEMENT_EMPTY", "请选择需要日结的班次", 422)
        collected = []
        for run_id in run_ids:
            archive = self.archives.store.fetch(run_id)
            manifest = archive[0]
            if manifest["result_sha256"] != json_sha(archive[1]):
                raise FieldError("SHIFT_LEDGER_DRIFT", f"班次原账摘要不符：{manifest['shift']}", 409)
            collected.append(archive)
        return collected

    def close_day(self, run_ids):
        # 先把各班原账全部读出并校验；任何一班缺失都会在此中止，尚无写入。
        archives = self._collect(run_ids)
        sustained = max(archive[0]["sustained_hours"] for archive in archives)
        report = settle(archives, sustained)
        report["policy"] = POLICY
        report["source_hashes"] = [{
            "run_id": manifest["id"], "shift": manifest["shift"],
            "result_sha256": manifest["result_sha256"],
            "hashes": manifest["hashes"],
        } for manifest, _, _ in archives]
        report["report_sha256"] = json_sha(report)
        settlement_id = json_sha([
            POLICY, sustained,
            [(item["run_id"], item["result_sha256"]) for item in report["source_hashes"]],
        ])
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        saved, created_flag, version = self.store.insert(report, settlement_id, created)
        return {"id": settlement_id, "report": saved, "reused": not created_flag, "version": version}

    def current(self, day):
        self._check_day(day)
        report = self.store.current_for_day(day)
        if report is None:
            raise FieldError("UNKNOWN_SETTLEMENT", "该闭馆日尚无日结", 404)
        return report

    def report(self, settlement_id):
        return self.store.fetch(settlement_id)

    def history(self):
        return self.store.history()

    def _check_day(self, day):
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except (ValueError, TypeError) as exc:
            raise FieldError("BAD_DAY", "请输入 YYYY-MM-DD 形式的闭馆日", 422) from exc
