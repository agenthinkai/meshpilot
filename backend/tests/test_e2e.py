"""End-to-end tests against a real API process, a separate test database and the
real local models. A small fake provider stands in for the cloud, so these also
prove what would and wouldn't have left the server.

Runs only when MESHPILOT_E2E=1; see backend/tests/README.md.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

if os.environ.get("MESHPILOT_E2E") != "1":
    pytest.skip("MESHPILOT_E2E isn't set", allow_module_level=True)

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
LOCAL_MODEL = "benchmark-qwen25-05b"
CLOUD_PORT = 8766
CLOUD_KEY = "test-cloud-key"
CARD_PROMPT = "My card is 4111 1111 1111 1111. Say hello."


class FakeCloud(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeCloud.received.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if body.get("stream"):
            events = [
                {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "from "}}]},
                {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "cloud"}}]},
                {"id": "c", "object": "chat.completion.chunk", "choices": [],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
            ]
            payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        else:
            payload = json.dumps({
                "id": "c", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "from cloud"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        data = payload.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def api():
    cloud = ThreadingHTTPServer(("127.0.0.1", CLOUD_PORT), FakeCloud)
    threading.Thread(target=cloud.serve_forever, daemon=True).start()
    env = {
        **os.environ,
        "MESHPILOT_ENV": "development",  # allows the fake provider's http:// address
        "CLOUD_ROUTING_ENABLED": "true",
        "CLOUD_API_BASE": f"http://127.0.0.1:{CLOUD_PORT}/v1",
        "CLOUD_API_KEY": CLOUD_KEY,
        "CLOUD_MODEL": "provider-model",
        "LOCAL_DEFAULT_MODEL": LOCAL_MODEL,
        "LOGIN_MAX_FAILURES": "3",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        env=env,
    )
    try:
        for _ in range(120):
            try:
                if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        else:
            pytest.fail("API did not start")
        yield BASE
    finally:
        process.terminate()
        process.wait(timeout=20)
        cloud.shutdown()


@pytest.fixture(scope="session")
def admin_headers(api):
    resp = httpx.post(f"{api}/api/v1/auth/login", timeout=30,
                      json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="session")
def api_key(api, admin_headers):
    resp = httpx.post(f"{api}/api/v1/auth/api-keys", json={"name": "e2e"}, headers=admin_headers, timeout=30)
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


@pytest.fixture
def key_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def set_policy(api, headers, **changes):
    policy = {"cloud_enabled": False, "on_sensitive": "local", "blocked_terms": [], "auto_cloud_min_chars": None, **changes}
    resp = httpx.put(f"{api}/api/v1/governance/policy", json=policy, headers=headers, timeout=30)
    assert resp.status_code == 200, resp.text


def chat(api, headers, model, content, **extra):
    return httpx.post(f"{api}/v1/chat/completions", headers=headers, timeout=180, json={
        "model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 12, "temperature": 0, **extra,
    })


# ── OpenAI compatibility ─────────────────────────────────────────────────────

def test_requires_authentication(api):
    resp = httpx.post(f"{api}/v1/chat/completions", json={"model": LOCAL_MODEL, "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


def test_rejects_invalid_key(api):
    resp = httpx.get(f"{api}/v1/models", headers={"Authorization": "Bearer mp_notarealkey"})
    assert resp.status_code == 401


def test_lists_models(api, key_headers):
    resp = httpx.get(f"{api}/v1/models", headers=key_headers, timeout=30)
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert LOCAL_MODEL in ids and "auto" in ids
    assert "demo-llama-1b" not in ids


def test_chat_completion_with_official_shape(api, key_headers):
    resp = chat(api, key_headers, LOCAL_MODEL, "Say hello.")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == LOCAL_MODEL
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"]
    assert data["usage"]["completion_tokens"] > 0
    assert resp.headers["x-meshpilot-route"] == "local"


def test_openai_sdk_works(api, api_key):
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(base_url=f"{api}/v1", api_key=api_key)
    reply = client.chat.completions.create(model=LOCAL_MODEL, max_tokens=8, temperature=0,
                                           messages=[{"role": "user", "content": "Say hello."}])
    assert reply.choices[0].message.content
    chunks = list(client.chat.completions.create(model=LOCAL_MODEL, max_tokens=8, temperature=0, stream=True,
                                                 messages=[{"role": "user", "content": "Say hello."}]))
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    with pytest.raises(openai.NotFoundError):
        client.chat.completions.create(model="nope", messages=[{"role": "user", "content": "hi"}])


def test_streaming_ends_with_done_and_hides_usage_unless_asked(api, key_headers):
    def collect(**extra):
        with httpx.stream("POST", f"{api}/v1/chat/completions", headers=key_headers, timeout=180, json={
            "model": LOCAL_MODEL, "messages": [{"role": "user", "content": "Say hello."}],
            "max_tokens": 8, "temperature": 0, "stream": True, **extra,
        }) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            return [l[6:] for l in resp.iter_lines() if l.startswith("data: ")]

    plain = collect()
    assert plain[-1] == "[DONE]"
    chunks = [json.loads(l) for l in plain[:-1]]
    assert "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c["choices"])
    assert not any(c.get("usage") for c in chunks)

    assert all(c["model"] == LOCAL_MODEL for c in chunks)

    with_usage = [json.loads(l) for l in collect(stream_options={"include_usage": True})[:-1]]
    assert with_usage[-1]["choices"] == [] and with_usage[-1]["usage"]["completion_tokens"] > 0


def test_text_completions_respect_max_tokens(api, key_headers):
    resp = httpx.post(f"{api}/v1/completions", headers=key_headers, timeout=60, json={
        "model": LOCAL_MODEL, "prompt": "The capital of France is", "max_tokens": 4, "temperature": 0,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["object"] == "text_completion" and data["model"] == LOCAL_MODEL
    assert data["choices"][0]["text"] and data["choices"][0]["finish_reason"] == "length"
    assert data["usage"]["completion_tokens"] == 4


def test_text_completion_streaming(api, key_headers):
    with httpx.stream("POST", f"{api}/v1/completions", headers=key_headers, timeout=60, json={
        "model": LOCAL_MODEL, "prompt": "The capital of France is", "max_tokens": 4, "temperature": 0, "stream": True,
    }) as resp:
        lines = [l[6:] for l in resp.iter_lines() if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(l) for l in lines[:-1]]
    assert chunks[0]["object"] == "text_completion"
    assert "".join(c["choices"][0]["text"] for c in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"


LONG_DOCUMENT = " ".join(f"Clause {i}: the fee is {i} dollars." for i in range(400)) + " What is the fee in clause 7?"


def test_too_long_prompt_is_refused_rather_than_silently_cut(api, key_headers):
    resp = chat(api, key_headers, LOCAL_MODEL, LONG_DOCUMENT)
    assert resp.status_code == 400
    assert "Shorten it" in resp.json()["error"]["message"]
    legacy = httpx.post(f"{api}/v1/completions", headers=key_headers, timeout=60,
                        json={"model": LOCAL_MODEL, "prompt": LONG_DOCUMENT, "max_tokens": 4})
    assert legacy.status_code == 400


def test_generation_is_limited_to_the_room_left(api, key_headers):
    resp = chat(api, key_headers, LOCAL_MODEL, "Count upward from one.", max_tokens=4000)
    assert resp.status_code == 200, resp.text
    assert resp.json()["usage"]["completion_tokens"] <= 4096


def test_long_document_is_read_in_full(api, key_headers):
    filler = " ".join(f"Clause {i}: the fee is {i} dollars." for i in range(100))
    document = f"{filler} The secret word is PINEAPPLE. {filler}"
    resp = chat(api, key_headers, LOCAL_MODEL, f"{document}\n\nWhat is the secret word? Reply with only that word.")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["usage"]["prompt_tokens"] > 2500
    assert "PINEAPPLE" in data["choices"][0]["message"]["content"].upper()


def test_simultaneous_requests_wait_instead_of_failing(api, key_headers):
    results = []

    def ask(i):
        results.append(chat(api, key_headers, LOCAL_MODEL, f"Name a colour. Request {i}.").status_code)

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [200, 200, 200, 200]


def test_too_long_prompt_is_refused_on_sync_endpoint(api, key_headers):
    resp = httpx.post(f"{api}/api/v1/inference/sync/{LOCAL_MODEL}", timeout=60,
                      headers={"X-API-Key": key_headers["Authorization"][7:]},
                      json={"prompt": LONG_DOCUMENT, "max_tokens": 4})
    assert resp.status_code == 502 and "Shorten it" in resp.json()["detail"]


@pytest.mark.parametrize("extra,fragment", [
    ({"tools": [{"type": "function", "function": {"name": "x"}}]}, "tools"),
    ({"n": 2}, "n=1"),
])
def test_unsupported_features_fail_clearly(api, key_headers, extra, fragment):
    resp = chat(api, key_headers, LOCAL_MODEL, "hi", **extra)
    assert resp.status_code == 400
    assert fragment in resp.json()["error"]["message"]


def test_validation_errors_use_openai_shape(api, key_headers):
    resp = httpx.post(f"{api}/v1/chat/completions", headers=key_headers, json={"model": LOCAL_MODEL}, timeout=30)
    assert resp.status_code == 400
    assert "messages" in resp.json()["error"]["message"]


def test_unknown_model_is_404(api, key_headers):
    assert chat(api, key_headers, "does-not-exist", "hi").status_code == 404


# ── Privacy gateway ──────────────────────────────────────────────────────────

def test_cloud_off_by_default_nothing_is_sent(api, admin_headers, key_headers):
    set_policy(api, admin_headers)
    FakeCloud.received.clear()
    assert chat(api, key_headers, "cloud", "Say hello.").status_code == 403
    assert chat(api, key_headers, "auto", "Say hello.").headers["x-meshpilot-route"] == "local"
    assert FakeCloud.received == []


def test_clean_request_reaches_cloud_with_only_scanned_fields(api, admin_headers, key_headers):
    set_policy(api, admin_headers, cloud_enabled=True)
    FakeCloud.received.clear()
    resp = httpx.post(f"{api}/v1/chat/completions", headers=key_headers, timeout=60, json={
        "model": "cloud", "max_tokens": 5, "user": "someone",
        "messages": [{"role": "user", "content": "Say hello.", "name": "alice"}],
        "metadata": {"secret": "4111 1111 1111 1111"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-meshpilot-route"] == "cloud"
    assert resp.json()["choices"][0]["message"]["content"] == "from cloud"
    sent = FakeCloud.received[-1]
    assert sent["auth"] == f"Bearer {CLOUD_KEY}"
    assert sent["body"]["model"] == "provider-model"
    assert sent["body"]["messages"] == [{"role": "user", "content": "Say hello."}]
    assert set(sent["body"]) <= {"model", "messages", "max_tokens", "temperature", "top_p", "stop", "seed", "response_format", "stream"}


def test_cloud_streaming_passes_through(api, admin_headers, key_headers):
    set_policy(api, admin_headers, cloud_enabled=True)
    with httpx.stream("POST", f"{api}/v1/chat/completions", headers=key_headers, timeout=60, json={
        "model": "cloud", "messages": [{"role": "user", "content": "Say hello."}], "stream": True,
    }) as resp:
        assert resp.headers["x-meshpilot-route"] == "cloud"
        lines = [l[6:] for l in resp.iter_lines() if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    assert "".join(json.loads(l)["choices"][0]["delta"].get("content", "") for l in lines[:-1]) == "from cloud"


def test_sensitive_request_stays_local_and_never_reaches_cloud(api, admin_headers, key_headers):
    set_policy(api, admin_headers, cloud_enabled=True)
    FakeCloud.received.clear()
    resp = chat(api, key_headers, "cloud", CARD_PROMPT)
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-meshpilot-route"] == "local"
    assert "credit_card" in resp.headers["x-meshpilot-reason"]
    assert FakeCloud.received == []


def test_sensitive_request_can_be_refused_instead(api, admin_headers, key_headers):
    set_policy(api, admin_headers, cloud_enabled=True, on_sensitive="block")
    FakeCloud.received.clear()
    assert chat(api, key_headers, "cloud", CARD_PROMPT).status_code == 403
    assert FakeCloud.received == []


def test_blocked_term_stays_local(api, admin_headers, key_headers):
    set_policy(api, admin_headers, cloud_enabled=True, blocked_terms=["Project Falcon"])
    FakeCloud.received.clear()
    assert chat(api, key_headers, "cloud", "Summarise project falcon.").headers["x-meshpilot-route"] == "local"
    assert FakeCloud.received == []


def test_sensitive_data_in_an_earlier_message_is_caught(api, admin_headers, key_headers):
    set_policy(api, admin_headers, cloud_enabled=True)
    FakeCloud.received.clear()
    resp = httpx.post(f"{api}/v1/chat/completions", headers=key_headers, timeout=180, json={
        "model": "cloud", "max_tokens": 5,
        "messages": [{"role": "system", "content": "Customer email is a@b.co"}, {"role": "user", "content": "Hi"}],
    })
    assert resp.headers["x-meshpilot-route"] == "local"
    assert FakeCloud.received == []


def test_auto_threshold_routes_long_clean_requests(api, admin_headers, key_headers):
    set_policy(api, admin_headers, cloud_enabled=True, auto_cloud_min_chars=50)
    assert chat(api, key_headers, "auto", "short").headers["x-meshpilot-route"] == "local"
    assert chat(api, key_headers, "auto", "word " * 20).headers["x-meshpilot-route"] == "cloud"
    set_policy(api, admin_headers)


def test_policy_needs_admin(api, key_headers):
    assert httpx.get(f"{api}/api/v1/governance/policy", headers=key_headers).status_code == 401


# ── Audit trail ──────────────────────────────────────────────────────────────

def events(api, headers, **params):
    return httpx.get(f"{api}/api/v1/governance/audit/events", params=params, headers=headers, timeout=30).json()


def test_prompts_are_not_stored(api, admin_headers, key_headers):
    set_policy(api, admin_headers)
    secret = "unique-phrase-for-the-audit-test"
    chat(api, key_headers, LOCAL_MODEL, f"{secret} and a@b.co")
    time.sleep(0.5)
    latest = events(api, admin_headers, limit=5)
    assert secret not in json.dumps(latest)
    routed = next(e for e in latest if e["event_type"] == "inference.routed")
    assert routed["prompt_fingerprint"] and routed["actor_key_prefix"].startswith("mp_")
    assert routed["sensitive"] == {"email": 1}


def test_outcome_is_recorded_with_token_counts(api, admin_headers, key_headers):
    chat(api, key_headers, LOCAL_MODEL, "Say hello.")
    time.sleep(0.5)
    done = next(e for e in events(api, admin_headers, limit=5) if e["event_type"] == "inference.completed")
    assert done["completion_tokens"] > 0 and done["route"] == "local"


def test_streamed_request_is_recorded(api, admin_headers, key_headers):
    with httpx.stream("POST", f"{api}/v1/chat/completions", headers=key_headers, timeout=180, json={
        "model": LOCAL_MODEL, "messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 6, "stream": True,
    }) as resp:
        list(resp.iter_lines())
    time.sleep(1)
    latest = events(api, admin_headers, limit=1)[0]
    assert latest["event_type"] == "inference.completed" and latest["completion_tokens"] == 6


def test_stream_records_real_metrics(api, admin_headers):
    history = httpx.get(f"{api}/api/v1/inference/history", params={"limit": 1}, headers=admin_headers, timeout=30).json()[0]
    assert history["status"] == "completed" and history["completion_tokens"] == 6 and history["throughput_tps"] > 0
    assert history["ttft_ms"] > 0


def test_blocked_request_is_recorded(api, admin_headers, key_headers):
    set_policy(api, admin_headers)
    chat(api, key_headers, "cloud", "Say hello.")
    assert events(api, admin_headers, route="blocked", limit=1)[0]["event_type"] == "inference.blocked"


def test_summary_counts_routes(api, admin_headers):
    summary = httpx.get(f"{api}/api/v1/governance/audit/summary", headers=admin_headers, timeout=30).json()
    assert summary["local"] > 0 and summary["cloud"] > 0 and summary["blocked"] > 0


def test_export_formats(api, admin_headers):
    csv_resp = httpx.get(f"{api}/api/v1/governance/audit/export", params={"format": "csv"}, headers=admin_headers, timeout=60)
    assert csv_resp.status_code == 200 and csv_resp.text.startswith("seq,created_at,event_type")
    jsonl = httpx.get(f"{api}/api/v1/governance/audit/export", params={"format": "jsonl"}, headers=admin_headers, timeout=60)
    assert json.loads(jsonl.text.splitlines()[0])["hash"]


def test_concurrent_writes_keep_the_chain_valid(api, admin_headers):
    def fail_login(i):
        httpx.post(f"{api}/api/v1/auth/login", json={"email": f"nobody{i}@example.com", "password": "x"},
                   headers={"X-Real-IP": f"10.9.8.{i}"}, timeout=60)
    threads = [threading.Thread(target=fail_login, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    result = httpx.get(f"{api}/api/v1/governance/audit/verify", headers=admin_headers, timeout=60).json()
    assert result["ok"] is True and result["events_checked"] > 12


# ── Fit report ───────────────────────────────────────────────────────────────

def run_fit_in_this_database(run_id):
    # The stack's real worker uses the real database and won't find this test's
    # row, so the run happens here, using the same code path.
    code = f"import asyncio; from workers.fit_worker import _run; asyncio.run(_run({run_id!r}))"
    subprocess.run([sys.executable, "-c", code], check=True, timeout=300, env=os.environ.copy())


def test_fit_report_end_to_end(api, admin_headers):
    suite = {
        "name": "e2e", "model_ids": [LOCAL_MODEL],
        "workload": {"requests_per_day": 1000, "avg_input_tokens": 100, "avg_output_tokens": 50},
        "cases": [
            {"task": "extract", "prompt": "Reply with exactly this word: paris", "max_tokens": 8, "expect": {"contains_all": [["paris"]]}},
            {"task": "extract", "prompt": "Reply with exactly this word: zzzz", "max_tokens": 8, "expect": {"contains_all": [["never-in-any-answer"]]}},
            {"task": "long", "prompt": LONG_DOCUMENT, "max_tokens": 8, "expect": {"contains_all": [["7"]]}},
        ],
    }
    created = httpx.post(f"{api}/api/v1/fit/runs", json=suite, headers=admin_headers, timeout=30)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    run_fit_in_this_database(run_id)

    run = httpx.get(f"{api}/api/v1/fit/runs/{run_id}", headers=admin_headers, timeout=30).json()
    assert run["status"] == "completed", run
    report = run["report"]
    model = report["models"][0]
    assert model["tasks"]["extract"]["accuracy"] == 0.5
    assert model["generation_tps"] > 0 and model["prompt_tps"] > 0 and model["memory_mb"] > 0
    assert model["economics"]["servers_needed"] >= 1
    assert report["cloud_monthly_usd"] > 0
    long_case = next(c for c in model["cases"] if c["task"] == "long")
    assert long_case["score"] == 0 and "Shorten it" in long_case["error"]
    assert any("too long" in c for c in report["caveats"])
    assert any("directional" in c for c in report["caveats"])

    assert httpx.delete(f"{api}/api/v1/fit/runs/{run_id}", headers=admin_headers, timeout=30).status_code == 204
    assert httpx.get(f"{api}/api/v1/fit/runs/{run_id}", headers=admin_headers, timeout=30).status_code == 404


def test_fit_suite_with_uncheckable_case_is_rejected(api, admin_headers):
    resp = httpx.post(f"{api}/api/v1/fit/runs", headers=admin_headers, timeout=30, json={
        "cases": [{"task": "x", "prompt": "hi", "expect": {}}],
    })
    assert resp.status_code == 422


def _psql(sql):
    url = os.environ["DATABASE_URL"].replace("+asyncpg", "")
    import psycopg
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql)


def test_database_refuses_changes(api, admin_headers):
    psycopg = pytest.importorskip("psycopg")
    for sql in ("UPDATE audit_events SET actor_email='x'", "DELETE FROM audit_events", "TRUNCATE audit_events"):
        with pytest.raises(psycopg.errors.RaiseException):
            _psql(sql)


def test_verify_detects_tampering(api, admin_headers):
    # Someone with database-owner rights bypasses the trigger and edits one row.
    _psql("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_change")
    _psql("UPDATE audit_events SET actor_email='forged@example.com' WHERE seq = (SELECT min(seq)+3 FROM audit_events)")
    _psql("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_change")
    result = httpx.get(f"{api}/api/v1/governance/audit/verify", headers=admin_headers, timeout=60).json()
    assert result["ok"] is False and result["first_bad_seq"] is not None and result["head_hash"] is None


# ── Security ─────────────────────────────────────────────────────────────────

def test_login_is_throttled_after_repeated_failures(api):
    email = f"throttle-{time.time()}@example.com"
    codes = [httpx.post(f"{api}/api/v1/auth/login", json={"email": email, "password": "wrong"},
                        headers={"X-Real-IP": "10.1.2.3"}, timeout=30).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


def test_cors_does_not_allow_other_sites(api):
    resp = httpx.options(f"{api}/v1/models", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
    })
    assert "access-control-allow-origin" not in resp.headers


def test_async_webhook_to_internal_address_is_refused(api, key_headers):
    resp = httpx.post(f"{api}/api/v1/inference/async/{LOCAL_MODEL}", headers={"X-API-Key": key_headers["Authorization"][7:]},
                      json={"prompt": "hi", "webhook_url": "http://docker-proxy:2375/containers/create"}, timeout=30)
    assert resp.status_code == 400


def test_pickle_upload_is_refused(api, admin_headers):
    resp = httpx.post(f"{api}/api/v1/models", headers=admin_headers, timeout=30,
                      data={"name": "bad"}, files={"file": ("evil.pt", b"not really a model")})
    assert resp.status_code == 400


def test_existing_sync_endpoint_still_works_and_is_audited(api, admin_headers, key_headers):
    resp = httpx.post(f"{api}/api/v1/inference/sync/{LOCAL_MODEL}", headers={"X-API-Key": key_headers["Authorization"][7:]},
                      json={"prompt": "Say hello.", "max_tokens": 6}, timeout=180)
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"]
    latest = events(api, admin_headers, limit=3)
    assert latest[0]["event_type"] == "inference.completed" and latest[1]["event_type"] == "inference.routed"


def test_api_container_has_no_docker_socket():
    assert not os.path.exists("/var/run/docker.sock")
