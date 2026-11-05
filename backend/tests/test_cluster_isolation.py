"""The suite must never reach a real cluster, wherever it runs (a CI pod is
in-cluster with a mounted ServiceAccount token)."""

import os

from app import pods
from app.main import create_app
from fastapi.testclient import TestClient


def test_in_cluster_env_is_cleared_by_the_autouse_fixture():
    for var in ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "NODE_NAME"):
        assert var not in os.environ
    assert not pods.in_cluster()


def test_create_app_never_builds_a_real_kube_client(tmp_path, monkeypatch, gateway):
    built: list[int] = []

    class Boom(pods.RealKubeApi):
        def __init__(self) -> None:
            built.append(1)
            raise AssertionError("a real kubernetes client was constructed")

    monkeypatch.setattr(pods, "RealKubeApi", Boom)
    monkeypatch.setattr(pods, "_kube_factory", Boom)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "iso.db"))
    with TestClient(create_app(gateway=gateway)):
        assert pods.sweep_pods(set()) == []
    assert built == []
