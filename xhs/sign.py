# xhs-mcp signature module
# Author: Wang
# License: Non-Commercial Learning Use Only
#
# Generates XHS API request signature headers via the third-party
# `xhshow` library (pure Python).  Replaces the previous Playwright +
# `window.mnsv2` injection scheme, which broke when XHS rotated its web
# bundle in late 2025 (the function name / arg shape changed and is no
# longer reachable from outside the bundle's closure).  See:
# https://github.com/Cloxl/xhshow

from typing import Any, Dict, Optional, Union

from xhshow import Xhshow

_signer = Xhshow()


def sign_request(
    uri: str,
    cookies: Union[Dict[str, str], str],
    data: Optional[Dict[str, Any]] = None,
    method: str = "POST",
) -> Dict[str, str]:
    """Generate complete signature headers for an XHS API request.

    Args:
        uri: API path, e.g. "/api/sns/web/v1/search/notes".
        cookies: Browser cookies as dict or raw cookie string.  xhshow
            reads `a1` (and other identity fields) directly from this.
        data: GET params dict or POST payload dict.  ``None`` is
            acceptable for parameter-less calls (signed as no params /
            empty body, matching xhshow's behaviour).
        method: "GET" or "POST".

    Returns:
        Dict with keys ``x-s``, ``x-s-common``, ``x-t``,
        ``x-b3-traceid``, ``x-xray-traceid`` — ready to merge into the
        outbound HTTP headers.
    """
    method_upper = method.upper()
    if method_upper not in ("GET", "POST"):
        raise ValueError(f"Unsupported HTTP method for signing: {method}")

    # Reject non-dict data explicitly.  Accepting a string here used to
    # silently degrade — the GET branch would sign the bare URI (dropping
    # the query) and the POST branch would sign an empty body — producing
    # signatures that look valid but don't match the request.  All
    # in-tree callers pass dicts; new callers should serialize themselves
    # at the HTTP layer, not here.
    if data is not None and not isinstance(data, dict):
        raise TypeError(
            f"sign_request expects data to be a dict or None, got "
            f"{type(data).__name__}"
        )

    if method_upper == "GET":
        return _signer.sign_headers(
            method="GET",
            uri=uri,
            cookies=cookies,
            params=data or {},
        )
    return _signer.sign_headers(
        method="POST",
        uri=uri,
        cookies=cookies,
        payload=data,
    )
