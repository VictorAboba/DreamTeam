"""The hub token (docs/SPEC.md §14 "Hub token"): what a hub reachable from other hosts asks of every request that does
not come from its own loopback, and how DreamTeam's own clients find it.

Inside the Docker sandbox the hub has to listen on 0.0.0.0: a published port reaches the container's interface, never
its loopback. Every other container on the same bridge then reaches it too, so `dreamteam sandbox` makes a fresh token
for each container, keeps it on the host, outside every folder it mounts, for `dreamteam console`, `tui`, `mcp` and
`status` (store_token), and gives the container only its SHA-256 (DIGEST_ENV): the container's environment never
holds the token itself, and a digest lets nobody in. The hub compares the digest of what a request sends with it.
TOKEN_ENV, the token itself, is for the clients on the host and for a hub started without `dreamteam sandbox` (docker
compose). DreamTeam takes both out of its os.environ at once (take_env_token): no process it starts inherits them.
Which token a client sends where is decided in client_tokens alone.
A peer on the hub's own loopback needs none. Anything on the hub's computer that forwards connections to it (a reverse
proxy, an ssh tunnel) needs an authentication of its own: its clients arrive from loopback too."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

TOKEN_ENV = "DREAMTEAM_HUB_TOKEN"            # the token: clients send it; a hub without DIGEST_ENV asks for it
DIGEST_ENV = "DREAMTEAM_HUB_TOKEN_SHA256"    # what `dreamteam sandbox` gives the hub: the token's SHA-256 (hex)
NO_TOKEN_ENV = "DREAMTEAM_HUB_NO_TOKEN"      # "1": serve on a non-loopback address without a token all the same
TOKENS_DIR_ENV = "DREAMTEAM_TOKENS_DIR"      # where `dreamteam sandbox` keeps its tokens (default ~/.dreamteam/hub-tokens)
QUERY_PARAM = "token"                        # `?token=` of a board URL: the hub turns it into a cookie
COOKIE_PREFIX = "dreamteam_hub_"
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
FROM_ENV = object()   # web.GuardMiddleware's default token: hub_digest()

_taken: str | None = None          # TOKEN_ENV as main() found it (take_env_token)
_taken_digest: str | None = None   # DIGEST_ENV as main() found it


class TokenRequired(RuntimeError):
    """A DreamTeam hub answers at `base_url` but refuses every token a client may send it (client_tokens): a client
    must not take it for a dead hub (it would start a second one on the same home). The text says what would help."""

    def __init__(self, base_url: str, *, user_url: str | None = None):
        base = _origin_url(base_url)
        if not _env_allowed(base, user_url):
            # (no command with this address: it comes from a hub.json, and whoever wrote it would get the token)
            text = (f"The DreamTeam hub at {base} asks for its token. DreamTeam sends a token only to a hub on this "
                    "computer's loopback (127.0.0.1, localhost, [::1]), where `dreamteam sandbox` publishes its hub, "
                    "or to an address typed with `dreamteam console --url`, and this address is neither. An address "
                    "read from a home's hub.json gets no token: the agents can write that file. Give `--url` only "
                    f"the address of a hub you started yourself, with {TOKEN_ENV} set to its token.")
        else:
            tried = [f"the token in {TOKEN_ENV}"] if env_token() else []
            if is_loopback_url(base) and stored_token(_port(base)):
                tried.insert(0, f"the token `dreamteam sandbox` stored in {_token_file(_port(base))}")
            text = (f"The DreamTeam hub at {base} " + (f"refused {' and '.join(tried)}" if tried else "asks for its token")
                    + f": set {TOKEN_ENV} to the token it was started with (`dreamteam sandbox` prints a new one at "
                    "every start).")
        super().__init__(text)
        self.base_url = base


def take_env_token() -> None:
    """Move TOKEN_ENV and DIGEST_ENV out of os.environ (main(), before anything starts a process): no command, MCP
    server, installer or git that DreamTeam starts inherits them, whatever filter its environment goes through.
    (Popping them leaves the environment this process was started with as it was: hence DIGEST_ENV, the only form of
    the token `dreamteam sandbox` passes.)"""
    global _taken, _taken_digest
    value = os.environ.pop(TOKEN_ENV, None)
    if value is not None:
        _taken = value.strip() or None
    value = os.environ.pop(DIGEST_ENV, None)
    if value is not None:
        _taken_digest = value.strip() or None


def env_token() -> str | None:
    """TOKEN_ENV of this process: a token its clients send (client_tokens), and the one a hub without DIGEST_ENV asks
    for."""
    return os.environ.get(TOKEN_ENV, "").strip() or _taken


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def hub_digest() -> str | None:
    """The SHA-256 of the token this hub asks for: DIGEST_ENV (`dreamteam sandbox`), else that of TOKEN_ENV. A
    DIGEST_ENV that is no SHA-256 asks for a token nobody has (never for none)."""
    raw = (os.environ.get(DIGEST_ENV, "").strip() or _taken_digest or "").lower()
    if raw:
        valid = len(raw) == 64 and all(c in "0123456789abcdef" for c in raw)
        return raw if valid else digest(secrets.token_urlsafe(32))
    token = env_token()
    return digest(token) if token else None


def no_token_allowed() -> bool:
    """NO_TOKEN_ENV: the user lets a hub on a non-loopback address go without a token."""
    return os.environ.get(NO_TOKEN_ENV, "").strip().lower() in ("1", "true", "yes")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def matches(given: str | None, token_digest: str) -> bool:
    """`given` is the token whose SHA-256 is `token_digest`, compared in constant time."""
    if not given:
        return False
    return hmac.compare_digest(digest(given).encode("ascii"), token_digest.encode("ascii"))


def cookie_name(token_digest: str) -> str:
    """The cookie of the hub whose token has this SHA-256: browsers send a cookie to every port of a host, so the
    hubs of two sandboxes on one machine must not overwrite each other's."""
    return COOKIE_PREFIX + token_digest[:12]


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address((text or "").strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)   # ::ffff:127.0.0.1 on a dual-stack socket
    return mapped or ip


def is_loopback(address: str) -> bool:
    ip = _ip(address)
    return ip is not None and ip.is_loopback


def same_ip(a: str, b: str) -> bool:
    ip_a, ip_b = _ip(a), _ip(b)
    return ip_a is not None and ip_a == ip_b


def peer_is_loopback(scope) -> bool:
    """The ASGI connection comes from the hub's own loopback (a peer that is not an IP address does not)."""
    client = scope.get("client")
    return bool(client) and is_loopback(str(client[0]))


# ---- clients on the host ------------------------------------------------------------------------
def tokens_dir() -> Path:
    return Path(os.environ.get(TOKENS_DIR_ENV) or os.path.expanduser("~/.dreamteam/hub-tokens"))


def _token_file(port: int) -> Path:
    return tokens_dir() / f"{int(port)}.json"


def _inside(folder: Path, mount: Path) -> bool:
    """`folder` is `mount` or below it: case-insensitively where the OS is (normcase), and by inode where Python does
    not know that the file system ignores case (macOS)."""
    target = os.path.normcase(str(mount))
    for candidate in (folder, *folder.parents):
        if os.path.normcase(str(candidate)) == target:
            return True
        with contextlib.suppress(OSError):
            if candidate.exists() and os.path.samefile(candidate, mount):
                return True
    return False


def store_token(port: int, token: str, mounted: Path, also_mounted: Iterable[Path] = ()) -> Path | None:
    """Keep the token of the sandbox published on 127.0.0.1:port for DreamTeam's clients on the host. Never inside a
    folder mounted into the container (`mounted`, the home, or one of `also_mounted`, such as the host's skills
    library: the agents would read it there): None then, and nothing is stored."""
    folder = tokens_dir().expanduser().resolve()
    mounted = Path(mounted).resolve()
    if any(_inside(folder, Path(m).expanduser().resolve()) for m in (mounted, *also_mounted)):
        return None
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    path = _token_file(port)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"port": int(port), "token": token, "home": str(mounted)}, fh)
    os.replace(tmp, path)
    return path


def stored_token(port: int) -> str | None:
    try:
        data = json.loads(_token_file(port).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def forget_token(port: int, token: str) -> None:
    """Remove the stored token of `port` if it is still `token` (never another sandbox's)."""
    if stored_token(port) == token:
        with contextlib.suppress(OSError):
            _token_file(port).unlink()


def _endpoint(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of `url`; ws/wss count as http/https (the stream of a hub is the same hub)."""
    try:
        parts = urllib.parse.urlsplit(url or "")
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None
    if not host:
        return None
    secure = parts.scheme in ("https", "wss")
    return ("https" if secure else "http", host, port or (443 if secure else 80))


def _origin_url(url: str) -> str:
    endpoint = _endpoint(url)
    if endpoint is None:
        return url
    scheme, host, port = endpoint
    return f"{scheme}://{f'[{host}]' if ':' in host else host}:{port}"


def _port(url: str) -> int:
    endpoint = _endpoint(url)
    return endpoint[2] if endpoint else 0


def is_loopback_url(url: str) -> bool:
    """The URL's host is a loopback name (LOOPBACK_NAMES): where a stored token may go, and what DreamTeam's clients
    reach directly, never through the environment's proxy (HTTP_PROXY...), which would see the token."""
    endpoint = _endpoint(url)
    return endpoint is not None and endpoint[1] in LOOPBACK_NAMES


def _env_allowed(url: str, user_url: str | None) -> bool:
    endpoint = _endpoint(url)
    return endpoint is not None and (endpoint[1] in LOOPBACK_NAMES or _endpoint(user_url or "") == endpoint)


_accepted: dict[tuple[str, str, int], str] = {}   # the token each hub took last (tried first while still allowed)


def client_tokens(url: str, *, user_url: str | None = None) -> list[str]:
    """The tokens DreamTeam's clients may send to the hub at `url`, in the order they try them. Every client looks
    them up for each request (or WebSocket connect) and tries the next one when the hub refuses (`refused`): a
    `dreamteam sandbox` started again on the same port has a new token while the console, the TUI or the
    `dreamteam mcp` proxy stay attached. This is the one place that decides where a token may go:

    - the token `dreamteam sandbox` stored for the URL's port (stored_token): only to a loopback URL (127.0.0.1,
      localhost, [::1]: where the sandbox publishes its hub), and so only to that port's own hub;
    - TOKEN_ENV (env_token): to a loopback URL, or to the hub address the user typed on the command line
      (`user_url`: `dreamteam console --url`; its scheme, host and port), whatever its host; the clients of a hub
      in their own process (the console of `serve`/`run`, the TUI's launcher) pass its own address as `user_url`.

    Every other URL gets none, above all one read from a hub.json: it sits in the folder the container mounts, the
    agents can write it, and it must not take a client's token anywhere else. The stored token comes before
    TOKEN_ENV (a stale one, left by a sandbox killed before it could remove it, costs one refused request), except
    that the token the hub took last goes first."""
    tokens = [stored_token(_port(url)) if is_loopback_url(url) else None,
              env_token() if _env_allowed(url, user_url) else None]
    allowed = [t for t in tokens if t]
    last = _accepted.get(_endpoint(url))  # type: ignore[arg-type]
    return list(dict.fromkeys([last, *allowed] if last in allowed else allowed))


def token_taken(url: str, token: str | None) -> None:
    """The hub at `url` took `token`: client_tokens offers it first while it is still allowed there."""
    endpoint = _endpoint(url)
    if token and endpoint is not None:
        _accepted[endpoint] = token


def refused(status: int, headers) -> bool:
    """The answer is a DreamTeam hub asking for its token (401 with `WWW-Authenticate: Bearer realm="DreamTeam"`),
    not some other server's 401."""
    return status == 401 and "dreamteam" in str(headers.get("WWW-Authenticate") or "").lower()


def client_token(url: str) -> str | None:
    """The token a client that cannot try another one sends to the hub at `url` (a browser URL)."""
    return next(iter(client_tokens(url)), None)


def auth_headers(url: str) -> dict[str, str]:
    token = client_token(url)
    return {"Authorization": f"Bearer {token}"} if token else {}


@functools.cache
def _direct_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(request: urllib.request.Request, timeout: float):
    """urllib.request.urlopen, but a loopback URL without the environment's proxy: a corporate HTTP_PROXY without
    NO_PROXY for loopback would get the request and its token (and cannot reach this computer's loopback anyway)."""
    if is_loopback_url(request.full_url):
        return _direct_opener().open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def open_hub(request: urllib.request.Request, timeout: float, *, user_url: str | None = None):
    """urllib.request.urlopen for DreamTeam's clients of a hub (cli.py, console.py): the request carries a token of
    client_tokens, looked up now, and goes again with the next one when the hub refuses it. TokenRequired when the
    hub refuses them all; every other error as urlopen raises it."""
    tokens: list[str | None] = [*client_tokens(request.full_url, user_url=user_url)] or [None]
    for index, token in enumerate(tokens):
        if token:  # (unredirected: urllib would copy a plain header to wherever a redirect points)
            request.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            response = _urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if not refused(exc.code, exc.headers):
                raise
            exc.close()
            if index + 1 < len(tokens):
                continue
            raise TokenRequired(request.full_url, user_url=user_url) from None
        token_taken(request.full_url, token)
        return response
    raise AssertionError("open_hub: no attempt")   # (the last attempt returns or raises)


@functools.cache
def _httpx_auth_type():
    import httpx2   # (here: it takes a noticeable part of a second to import, and most commands never need it)

    class HubTokenAuth(httpx2.Auth):
        """The hub token of DreamTeam's async clients (the TUI's HubClient, `dreamteam mcp` as a proxy), as
        open_hub does it: looked up for every request, the next candidate after a refusal."""

        requires_request_body = True   # (the request may go twice)

        def __init__(self, user_url: str | None):
            self.user_url = user_url

        def auth_flow(self, request):
            url = str(request.url)
            for token in client_tokens(url, user_url=self.user_url) or [None]:
                if token:
                    request.headers["Authorization"] = f"Bearer {token}"
                response = yield request
                if not refused(response.status_code, response.headers):
                    token_taken(url, token)
                    return

    return HubTokenAuth


def httpx_auth(user_url: str | None = None):
    """The httpx2.Auth that sends the hub token (client_tokens) with every request of an httpx2 client."""
    return _httpx_auth_type()(user_url)


def browser_url(url: str, *, user_url: str | None = None) -> str:
    """`url` with `?token=` when its hub has a token known here (client_tokens): the hub sets its cookie and
    redirects to `url`."""
    token = next(iter(client_tokens(url, user_url=user_url)), None)
    if not token:
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode([*urllib.parse.parse_qsl(parts.query, keep_blank_values=True),
                                    (QUERY_PARAM, token)])
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, parts.fragment))


__all__ = ["DIGEST_ENV", "NO_TOKEN_ENV", "TOKEN_ENV", "TOKENS_DIR_ENV", "TokenRequired", "auth_headers", "browser_url",
           "client_token", "client_tokens", "cookie_name", "digest", "env_token", "forget_token", "httpx_auth",
           "hub_digest", "is_loopback_url", "matches", "new_token", "no_token_allowed", "open_hub",
           "peer_is_loopback", "refused",
           "store_token", "stored_token", "take_env_token", "token_taken"]
