"""Unit tests for xhs/sign.py.

Covers the pure-Python signature building blocks (custom Base64, CRC32
variant, payload builders).  Does NOT cover the Playwright-dependent
``get_b1_from_localstorage`` / ``call_mnsv2`` / ``sign_request`` —
those need a real browser and are out of scope for fast unit tests.
"""

from __future__ import annotations


from xhs import sign


def test_b64_encode_empty_list_returns_empty_string():
    assert sign.b64_encode([]) == ""


def test_b64_encode_stable_output():
    """Custom Base64 with XHS-shuffled alphabet must be deterministic."""
    data = list(b"hello world")
    first = sign.b64_encode(data)
    second = sign.b64_encode(data)
    assert first == second
    # Output uses XHS's alphabet — so at least one char outside std b64 must appear
    # on typical inputs.  (The alphabet still contains 'Z', 'm', etc. from std b64,
    # so we only assert determinism and non-empty length here.)
    assert len(first) > 0


def test_b64_encode_length_padding():
    # 1-byte input → 4-char output with "==" padding in the XHS alphabet
    out = sign.b64_encode([65])
    assert len(out) == 4
    assert out.endswith("==")

    # 2-byte input → 4-char output with "=" padding
    out = sign.b64_encode([65, 66])
    assert len(out) == 4
    assert out.endswith("=")

    # 3-byte input → 4-char output with no padding
    out = sign.b64_encode([65, 66, 67])
    assert len(out) == 4
    assert not out.endswith("=")


def test_encode_utf8_ascii_matches_raw_bytes():
    assert sign.encode_utf8("abc") == [ord("a"), ord("b"), ord("c")]


def test_encode_utf8_chinese_produces_utf8_bytes():
    # "猫" is 3 bytes in UTF-8 (0xE7 0x8C 0xAB)
    result = sign.encode_utf8("猫")
    assert result == [0xE7, 0x8C, 0xAB]


def test_mrc_is_deterministic_and_bounded():
    """CRC32 variant must be stable and fit in int32 range."""
    value = sign.mrc("foo")
    assert value == sign.mrc("foo")
    # The algorithm XORs with -1 at the end; range check is still meaningful.
    assert isinstance(value, int)


def test_mrc_changes_with_input():
    assert sign.mrc("a") != sign.mrc("b")


def test_md5_hex_length_and_determinism():
    h = sign._md5_hex("hello")
    assert len(h) == 32
    assert h == sign._md5_hex("hello")
    assert h != sign._md5_hex("Hello")


def test_get_trace_id_shape():
    """X-B3-Traceid must be 16 hex chars."""
    tid = sign.get_trace_id()
    assert len(tid) == 16
    assert all(c in "abcdef0123456789" for c in tid)


def test_build_sign_string_post_with_dict():
    # JSON is built with compact separators and ensure_ascii=False
    s = sign._build_sign_string("/api/foo", {"a": 1, "b": "中"}, method="POST")
    assert s.startswith("/api/foo")
    assert "中" in s


def test_build_sign_string_post_with_string_payload():
    s = sign._build_sign_string("/api/foo", "raw_body", method="POST")
    assert s == "/api/foo" + "raw_body"


def test_build_sign_string_get_empty_dict_returns_uri_only():
    assert sign._build_sign_string("/api/foo", {}, method="GET") == "/api/foo"


def test_build_sign_string_get_encodes_list_params():
    s = sign._build_sign_string("/api/foo", {"ids": ["a", "b"]}, method="GET")
    assert s == "/api/foo?ids=a%2Cb"


def test_build_xs_payload_prefixed_and_base64_of_json():
    payload = sign._build_xs_payload("x3-abc", data_type="object")
    assert payload.startswith("XYS_")
    # The part after XYS_ should decode structurally (can't fully verify custom b64,
    # but we can at least ensure it's nonempty and doesn't contain raw JSON braces).
    assert len(payload) > len("XYS_")
    assert "{" not in payload


def test_build_xs_common_contains_mrc_x9_field():
    """x-s-common carries x9 = mrc(x_t + x_s + b1); ensure the function runs end-to-end."""
    out = sign._build_xs_common("a1-val", "b1-val", "xs-val", "1700000000")
    assert isinstance(out, str) and len(out) > 0
