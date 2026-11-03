import pytest

from core.privacy import BLOCKED, CLOUD, LOCAL, decide_route, messages_text, scan_text


@pytest.mark.parametrize("text,category", [
    ("mail me at priya.n@example.co.uk please", "email"),
    ("card 4111 1111 1111 1111 expires soon", "credit_card"),
    ("card 4111-1111-1111-1111", "credit_card"),
    ("IBAN GB82 WEST 1234 5698 7654 32", "iban"),
    ("SSN 078-05-1120", "us_ssn"),
    ("Emirates ID 784-1990-1234567-1", "emirates_id"),
    ("Aadhaar 2341 2341 2346", "aadhaar"),
    ("PAN ABCDE1234F", "india_pan"),
    ("call +971 50 123 4567", "phone"),
    ("call (415) 555-0132", "phone"),
    ("mobile 9876543210", "phone"),
    ("account 0012345678901", "account_number"),
    ("key sk-abcdefghijklmnopqrstuvwx", "credential"),
    ("password: hunter2", "credential"),
])
def test_detects_sensitive_data(text, category):
    assert category in scan_text(text)


@pytest.mark.parametrize("text", [
    "The outage lasted 3 hours 18 minutes on 14 March 2026.",
    "Revenue grew 12% to AED 486,250 in 2025.",
    "Explain the key risks of a SaaS business in three bullet points.",
    "Reference INC-40721 was closed at 13:00.",
])
def test_ordinary_text_is_clean(text):
    assert scan_text(text) == {}


def test_checksums_filter_lookalikes():
    assert "credit_card" not in scan_text("4111 1111 1111 1112")
    assert "iban" not in scan_text("GB82 WEST 1234 5698 7654 33")


def test_results_never_contain_the_data():
    found = scan_text("card 4111 1111 1111 1111")
    assert "4111" not in str(found)


def test_blocked_terms_ignore_case():
    assert scan_text("update on PROJECT falcon", ["Project Falcon"]) == {"blocked_term": 1}
    assert scan_text("nothing here", ["Project Falcon", ""]) == {}


def test_messages_text_flags_non_text_content():
    text, ok = messages_text([{"role": "user", "content": "hello"}])
    assert (text, ok) == ("hello", True)
    text, ok = messages_text([{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}])
    assert text == "what is this" and ok is False


def route(model="cloud", text="hello", scannable=True, available=True, policy=True,
          on_sensitive="local", terms=(), auto_chars=None):
    return decide_route(model, text, scannable, available, policy, on_sensitive, terms, auto_chars)


def test_clean_cloud_request_goes_to_cloud():
    assert route().route == CLOUD


def test_named_local_model_always_stays_local():
    assert route(model="benchmark-qwen25-05b").route == LOCAL


@pytest.mark.parametrize("available,policy", [(False, True), (True, False), (False, False)])
def test_both_switches_are_required(available, policy):
    assert route(available=available, policy=policy).route == BLOCKED
    assert route(model="auto", auto_chars=1, available=available, policy=policy).route == LOCAL


def test_sensitive_data_never_reaches_cloud():
    text = "card 4111 1111 1111 1111"
    kept = route(text=text)
    assert kept.route == LOCAL and "credit_card" in kept.sensitive
    assert route(text=text, on_sensitive="block").route == BLOCKED
    assert route(model="auto", text=text, auto_chars=1).route == LOCAL


def test_blocked_term_never_reaches_cloud():
    assert route(text="the Falcon deal", terms=["falcon"]).route == LOCAL


def test_unscannable_content_never_reaches_cloud():
    assert route(scannable=False).route == LOCAL
    assert route(scannable=False, on_sensitive="block").route == BLOCKED


def test_auto_uses_length_threshold():
    assert route(model="auto", text="short").route == LOCAL
    assert route(model="auto", text="short", auto_chars=100).route == LOCAL
    assert route(model="auto", text="x" * 100, auto_chars=100).route == CLOUD
