from fastapi import APIRouter, Request

from ..schemas import ModelsOut
from ..settings_store import load_settings

router = APIRouter(tags=["models"])


@router.get("/models", response_model=ModelsOut)
def list_models(request: Request) -> ModelsOut:
    described = request.app.state.registry.describe()
    # an explicitly pinned default (Application settings) wins over the
    # cheapest-keyed-medium heuristic
    pinned = load_settings(request.app.state.engine).get("default_model")
    if pinned:
        described["default_model"] = pinned
    return ModelsOut.model_validate(described)
