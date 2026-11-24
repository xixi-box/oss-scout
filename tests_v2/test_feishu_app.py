import io
import json
from urllib.error import HTTPError

import pytest

from scout_v2 import feishu_app


RECORDS = "https://open.feishu.cn/open-apis/bitable/v1/apps/private-app/tables/private-table/records/batch_update?secret=cursor"


class Response(io.BytesIO):
    status = 200


@pytest.fixture
def feishu(monkeypatch):
    clock = [100.0]
    calls = []
    replies = []

    def urlopen(request, *, timeout):
        calls.append(request)
        status, body = replies.pop(0)
        response = Response(json.dumps(body).encode())
        if status != 200:
            raise HTTPError(request.full_url, status, "Bad Request", {}, response)
        return response

    monkeypatch.setattr(feishu_app, "_TOKEN_CACHE", (0.0, ""))
    monkeypatch.setattr(feishu_app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(feishu_app, "_app_credentials", lambda: ("app", "private-secret"))
    monkeypatch.setattr(feishu_app.urllib.request, "urlopen", urlopen)
    return clock, calls, replies


@pytest.mark.parametrize("expiry_key", ["expire", "expires_in"])
def test_refresh_before_reported_expiry(feishu, expiry_key):
    clock, calls, replies = feishu
    replies.extend([(200, {"tenant_access_token": "first", expiry_key: 60}),
                    (200, {"tenant_access_token": "second", expiry_key: 120}),
                    (200, {"code": 0})])
    token = feishu_app.tenant_token()
    clock[0] = 153
    assert feishu_app.tenant_token() == token == "first"
    clock[0] = 154
    assert feishu_app._request("POST", RECORDS, {}, token=token) == {"code": 0}
    assert len(calls) == 3
    assert calls[-1].get_header("Authorization") == "Bearer second"


def test_missing_expiry_never_caches_token(feishu):
    _, calls, replies = feishu
    replies.extend([(200, {"tenant_access_token": "first"}),
                    (200, {"tenant_access_token": "second", "expire": 0})])
    assert feishu_app.tenant_token() == "first"
    assert feishu_app.tenant_token() == "second"
    assert len(calls) == 2


@pytest.mark.parametrize(("status", "code"), [(400, 99991663), (200, 99991665)])
def test_rejected_token_refreshes_and_retries_same_mutation(feishu, caplog, status, code):
    _, calls, replies = feishu
    replies.extend([(200, {"tenant_access_token": "first", "expire": 60}),
                    (status, {"code": code, "msg": "private-response"}),
                    (200, {"tenant_access_token": "second", "expire": 120}),
                    (200, {"code": 0}),
                    (200, {"code": 0})])
    token = feishu_app.tenant_token()
    assert feishu_app._request("POST", RECORDS, {"records": []}, token=token) == {"code": 0}
    assert feishu_app._request("POST", RECORDS, {"records": []}, token=token) == {"code": 0}
    assert len(calls) == 5
    assert calls[1].data == calls[3].data == calls[4].data
    assert [calls[i].get_header("Authorization") for i in (1, 3, 4)] == [
        "Bearer first", "Bearer second", "Bearer second",
    ]
    assert f"POST /open-apis/bitable/v1/apps/{{id}}/tables/{{id}}/records/batch_update HTTP {status} code={code}" in caplog.text
    for private in ("private-app", "private-table", "private-response", "secret=cursor", "Bearer"):
        assert private not in caplog.text


@pytest.mark.parametrize("code", [99991663, 1254060, None])
def test_http_error_only_retries_known_auth_rejection(feishu, caplog, code):
    _, calls, replies = feishu
    invalid_response = (400, {"code": code})
    replies.extend([(200, {"tenant_access_token": "first", "expire": 60}), invalid_response])
    if code == 99991663:
        replies.extend([(200, {"tenant_access_token": "second", "expire": 120}), invalid_response])
    with pytest.raises(HTTPError, match=f"code={code if code is not None else 'unavailable'}"):
        feishu_app._request("POST", RECORDS, {}, token=feishu_app.tenant_token())
    assert len(calls) == (4 if code == 99991663 else 2)
    assert caplog.text.count("refreshing credential and retrying") == (1 if code == 99991663 else 0)
