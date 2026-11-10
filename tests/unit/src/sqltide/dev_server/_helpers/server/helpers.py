"""An in-process HTTP client for the native dev server application."""

import json
from collections.abc import Mapping, Sequence
from email.message import Message
from http.cookies import Morsel, SimpleCookie
from types import TracebackType
from typing import Any, NotRequired, Self, TypedDict, Unpack
from urllib.parse import SplitResult, unquote, urlencode, urlsplit

from sqltide._native import DevApplication

_CLIENT_HOST: str = "testclient"
_DEFAULT_HEADERS: dict[str, str] = {"host": "testserver", "user-agent": "testclient"}
# Indexed by whether the request has no JSON body.
_BODY_HEADERS: dict[bool, dict[str, str]] = {False: {"content-type": "application/json"}, True: {}}


class NativeResponse:
    """One response, with the accessors the tests read."""

    def __init__(self, *, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status_code: int = status
        self.headers: Message = Message()
        for name, value in headers:
            self.headers[name] = value
        self.content: bytes = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:  # noqa: ANN401 - decoded JSON, as the earlier client returned it
        return json.loads(self.content)


class _RequestOptions(TypedDict):
    params: NotRequired[Mapping[str, object] | Sequence[tuple[str, object]]]
    json: NotRequired[object]
    headers: NotRequired[Mapping[str, str]]


class NativeTestClient:
    """Send requests to a `DevApplication` in process and keep its session cookies."""

    def __init__(self, app: DevApplication) -> None:
        self.app: DevApplication = app
        self.jar: SimpleCookie = SimpleCookie()

    def __enter__(self) -> Self:
        self.app.startup()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.app.shutdown()

    @property
    def cookies(self) -> dict[str, str]:
        """Cookies the server set and has not expired with `Max-Age=0`."""

        live: filter[tuple[str, Morsel]] = filter(
            lambda item: item[1]["max-age"] != "0", self.jar.items()
        )
        return {name: morsel.value for name, morsel in live}

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | Sequence[tuple[str, object]] = (),
        json: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> NativeResponse:
        parts: SplitResult = urlsplit(url)
        query: str = "&".join(filter(None, (parts.query, urlencode(params, doseq=True))))
        sent: dict[str, str] = {
            **_DEFAULT_HEADERS,
            **_BODY_HEADERS[json is None],
            **{name.lower(): value for name, value in (headers or {}).items()},
        }
        sent.setdefault(
            "cookie", "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        )
        status, reply_headers, reply_body = self.app.request(
            method,
            unquote(parts.path) or "/",
            query_string=query,
            headers=list(sent.items()),
            body=_BODIES[json is None](json),
            client=_CLIENT_HOST,
        )
        response: NativeResponse = NativeResponse(
            status=status, headers=list(reply_headers), body=bytes(reply_body)
        )
        for header in response.headers.get_all("set-cookie", ()):
            self.jar.load(header)
        return response

    def get(self, url: str, **options: Unpack[_RequestOptions]) -> NativeResponse:
        return self.request("GET", url, **options)

    def post(self, url: str, **options: Unpack[_RequestOptions]) -> NativeResponse:
        return self.request("POST", url, **options)

    def patch(self, url: str, **options: Unpack[_RequestOptions]) -> NativeResponse:
        return self.request("PATCH", url, **options)

    def delete(self, url: str, **options: Unpack[_RequestOptions]) -> NativeResponse:
        return self.request("DELETE", url, **options)


def _dumps(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


# Indexed by whether the request has no JSON body.
_BODIES: dict[bool, Any] = {False: _dumps, True: lambda _value: b""}
