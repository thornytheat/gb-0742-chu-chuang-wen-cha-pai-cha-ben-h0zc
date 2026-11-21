from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.dependencies import SettlementSvc

router = APIRouter(prefix="/api/settlements")


class CloseDayRequest(BaseModel):
    run_ids: list[str] = Field(min_length=2, max_length=16)


@router.get("")
def history(service: SettlementSvc):
    return service.history()


@router.post("", status_code=201)
def close_day(body: CloseDayRequest, service: SettlementSvc, response: Response):
    result = service.close_day(body.run_ids)
    if result["reused"]:
        response.status_code = 200
    return {"id": result["id"], "reused": result["reused"], "version": result["version"],
            "report": result["report"]}


@router.get("/days/{day}")
def current(day: str, service: SettlementSvc):
    return service.current(day)


@router.get("/{settlement_id}")
def report(settlement_id: str, service: SettlementSvc):
    return service.report(settlement_id)
