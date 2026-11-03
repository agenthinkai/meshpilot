"""The proxy lives in infra/; tests run with it mounted at /docker-proxy."""
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

_path = Path(os.environ.get("DOCKER_PROXY_SOURCE", "/docker-proxy/proxy.py"))
if not _path.exists():
    pytest.skip("infra/docker-proxy isn't mounted", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("docker_proxy", _path)
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)


def good_payload():
    return {
        "Image": "meshpilot-llamacpp",
        "Env": ["LLAMA_MODEL_DIR=/models", "LLAMA_HOST=0.0.0.0", "LLAMA_PORT=8080", "LLAMA_THREADS=4",
                "LLAMA_CTX_SIZE=4096", "LLAMA_BATCH_SIZE=512", "LLAMA_PARALLEL=1", "LLAMA_MODEL_FILE=qwen.gguf"],
        "HostConfig": {"CpusetCpus": "8-11", "Memory": 1073741824,
                       "Binds": ["meshpilot_model-storage:/models:ro"],
                       "NetworkMode": "meshpilot_default", "RestartPolicy": {"Name": "no"}},
    }


def name_filter(pattern):
    return quote(json.dumps({"name": [pattern]}))


@pytest.mark.parametrize("method,target", [
    ("GET", f"/containers/json?all=true&filters={name_filter('^/meshpilot-dyn-qwen$')}"),
    ("POST", "/containers/create?name=meshpilot-dyn-qwen"),
    ("POST", "/containers/meshpilot-dyn-qwen/start"),
    ("POST", "/containers/meshpilot-dyn-qwen/stop?t=10"),
    ("DELETE", "/containers/meshpilot-dyn-qwen?force=true&v=true"),
    ("GET", "/containers/meshpilot-dyn-qwen/stats?stream=false"),
    ("GET", "/containers/meshpilot-pytorch-baseline/stats?stream=false"),
])
def test_orchestrator_calls_are_allowed(method, target):
    proxy.authorize(method, target)


@pytest.mark.parametrize("method,target", [
    ("GET", "/containers/json"),
    ("GET", "/containers/json?all=true"),
    ("GET", f"/containers/json?filters={name_filter('^/meshpilot-api$')}"),
    ("GET", f"/containers/json?filters={name_filter('.*')}"),
    ("POST", "/containers/create?name=evil"),
    ("POST", "/containers/create"),
    ("POST", "/containers/meshpilot-api/stop"),
    ("POST", "/containers/meshpilot-postgres/start"),
    ("DELETE", "/containers/meshpilot-postgres?force=true"),
    ("POST", "/containers/meshpilot-dyn-qwen/exec"),
    ("POST", "/containers/meshpilot-dyn-qwen/kill"),
    ("GET", "/containers/meshpilot-postgres/json"),
    ("GET", "/containers/meshpilot-postgres/stats?stream=false"),
    ("GET", "/containers/meshpilot-postgres/logs"),
    ("GET", "/containers/meshpilot-dyn-qwen/archive?path=/etc/passwd"),
    ("POST", "/images/create?fromImage=evil"),
    ("POST", "/volumes/create"),
    ("POST", "/networks/create"),
    ("GET", "/secrets"),
    ("GET", "/version"),
    ("POST", "/v1.41/containers/create?name=meshpilot-dyn-qwen"),
    ("GET", "/containers/meshpilot-dyn-qwen%2F..%2Fmeshpilot-api/stats?stream=false"),
])
def test_everything_else_is_denied(method, target):
    with pytest.raises(proxy.Denied):
        proxy.authorize(method, target)


def test_valid_create_payload_passes():
    assert proxy.validate_create_payload(good_payload())


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(Image="alpine"),
    lambda p: p.update(Cmd=["sh", "-c", "id"]),
    lambda p: p.update(Entrypoint=["sh"]),
    lambda p: p.update(User="root"),
    lambda p: p["Env"].append("LD_PRELOAD=/tmp/x.so"),
    lambda p: p["Env"].append("LLAMA_MODEL_FILE=../../etc/passwd"),
    lambda p: p["HostConfig"].update(Privileged=True),
    lambda p: p["HostConfig"].update(CapAdd=["SYS_ADMIN"]),
    lambda p: p["HostConfig"].update(PidMode="host"),
    lambda p: p["HostConfig"].update(Devices=[{"PathOnHost": "/dev/sda"}]),
    lambda p: p["HostConfig"].update(Binds=["/:/host"]),
    lambda p: p["HostConfig"].update(Binds=["/var/run/docker.sock:/var/run/docker.sock"]),
    lambda p: p["HostConfig"].update(Binds=["meshpilot_model-storage:/models:rw"]),
    lambda p: p["HostConfig"].update(NetworkMode="host"),
    lambda p: p["HostConfig"].update(RestartPolicy={"Name": "always"}),
    lambda p: p["HostConfig"].update(Memory=0),
    lambda p: p["HostConfig"].update(Memory="1g"),
    lambda p: p["HostConfig"].update(CpusetCpus="0-3;rm"),
])
def test_dangerous_create_payloads_are_denied(mutate):
    payload = good_payload()
    mutate(payload)
    with pytest.raises(proxy.Denied):
        proxy.validate_create_payload(payload)


def test_non_object_payload_is_denied():
    with pytest.raises(proxy.Denied):
        proxy.validate_create_payload(["Image"])
