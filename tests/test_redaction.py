from harnesslab.trace.redaction import Redactor, redact_text, secret_env_values


def test_provider_keys_are_redacted():
    text = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789 and sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    out = redact_text(text)
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in out
    assert "sk-ant-api03" not in out
    assert "OPENAI_API_KEY=" in out
    assert "[REDACTED:" in out


def test_github_bearer_aws_and_misc_tokens():
    text = (
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab; github_pat_11ABCDEFG0123456789_abcdefghijklmnop; "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def; AKIAIOSFODNN7EXAMPLE; "
        "xoxb-1234567890-abcdefghij; AIzaSyA-1234567890abcdefghijklmnopqrstu"
    )
    out = redact_text(text)
    for secret in (
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab",
        "github_pat_11ABCDEFG",
        "eyJhbGciOiJIUzI1NiJ9",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1234567890",
        "AIzaSyA-1234567890",
    ):
        assert secret not in out, secret
    assert "Authorization: Bearer [REDACTED:bearer_token]" in out


def test_secret_assignments_keep_names_and_skip_token_counts():
    out = redact_text(
        'ANTHROPIC_API_KEY="abcdefghijklmnop"\naws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"api_key": "supersecretvalue"'
    )
    assert 'ANTHROPIC_API_KEY="[REDACTED:secret_assignment]"' in out
    assert "wJalrXUtnFEMI" not in out
    assert "supersecretvalue" not in out
    # Metrics-like text must survive untouched.
    benign = "input_tokens=1234 output_tokens: 567 max_tokens=8192 sha=0123456789abcdef0123456789abcdef01234567"
    assert redact_text(benign) == benign


def test_private_key_block():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----\nrest"
    out = redact_text(text)
    assert "MIIEow" not in out and out.endswith("rest")


def test_env_literal_values(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "literal-secret-value-123")
    monkeypatch.setenv("SHORT_KEY", "abc")
    assert "literal-secret-value-123" in secret_env_values()
    assert "abc" not in secret_env_values()
    r = Redactor()
    assert "literal-secret-value-123" not in r.redact_text("x literal-secret-value-123 y")


def test_redact_value_recurses_and_keeps_structure():
    r = Redactor(include_process_env=False)
    value = {
        "cmd": "curl -H 'Authorization: Bearer abcdefgh12345'",
        "n": 3,
        "list": ["ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", 1.5],
        "nested": {"ok": True},
    }
    out = r.redact_value(value)
    assert out["n"] == 3 and out["nested"] == {"ok": True} and out["list"][1] == 1.5
    assert "abcdefgh12345" not in out["cmd"] and "ghp_" not in out["list"][0]
    assert r.redaction_count >= 2
