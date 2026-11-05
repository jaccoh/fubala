from fastapi import APIRouter

from ..schemas import PingOut

router = APIRouter(tags=["system"])


@router.get("/ping", response_model=PingOut)
def ping() -> PingOut:
    return PingOut(ping="pong")
