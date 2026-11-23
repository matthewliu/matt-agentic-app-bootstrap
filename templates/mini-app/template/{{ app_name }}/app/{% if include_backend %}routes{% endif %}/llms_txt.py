"""GET /llms.txt and GET /llms-openapi.json: this app's public pages and public API.

llms.txt is a plain Markdown file at the site root that tells AI tools what
a site contains (spec: https://llmstxt.org). It does not change search or AI
answer rankings. It helps coding agents find public API and page docs.

Both routes are off by default. They return 404 until LLMS_TXT_PUBLIC_URL
holds the app's canonical public URL, for example `https://my-app.com`. They
also return 404 on every other host, so a Render preview or the `onrender.com`
host of an app with a custom domain does not serve them. The host check is not
access control: a client can send any `Host` header. The content is public by
design; the check only stops duplicate copies on other hosts.

`/llms-openapi.json` is an OpenAPI document that holds the public routes only.
The full `/openapi.json` lists every route, including protected ones, so
llms.txt never links to it.

Both are built from the running app on every request, so they stay current:

- An API route appears only when it is marked with `openapi_extra=PUBLIC_DOC`
  AND it does not depend on `validate_token`. A marked route that requires
  sign-in is left out (fail closed), and
  tests/integration/test_llms_txt.py fails on it.
- A page appears only when it is listed in PUBLIC_PAGES. The backend cannot
  see which React routes need sign-in, so list only pages that a signed-out
  visitor can open.

Do not mark a route that checks sign-in by itself instead of through
`validate_token`. The check here cannot see that kind of route.
"""

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import fastapi.routing
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.dependencies.models import Dependant
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.security.base import SecurityBase

from app.auth.middleware import validate_token
from app.config import llms_txt_settings

logger = logging.getLogger(__name__)
router = APIRouter()

PATH = "/llms.txt"
OPENAPI_PATH = "/llms-openapi.json"
# Mark a public API route for llms.txt: `@router.get(..., openapi_extra=PUBLIC_DOC)`.
MARKER = "x-llms-txt"
PUBLIC_DOC: dict[str, Any] = {MARKER: True}
DEFAULT_PORTS = {"http": "80", "https": "443"}
CACHE_CONTROL = "public, max-age=3600"
# A page path: exactly one leading slash, then URL-safe characters only.
# Spaces, parentheses, and control characters would break the Markdown link.
PAGE_PATH_RE = re.compile(r"/(?!/)[A-Za-z0-9._~/%-]*\Z")


@dataclass(frozen=True)
class PublicPage:
    """A frontend page that a signed-out visitor can open."""

    path: str
    title: str
    description: str = ""


# Add the pages a signed-out visitor can open, for example
# PublicPage("/pricing", "Pricing", "Plans and limits."). The sign-in page is
# not listed: it holds no content for an agent.
PUBLIC_PAGES: list[PublicPage] = []


def _normalize_host(scheme: str, host: str) -> str:
    """Lowercase the host and drop the default port for the scheme."""
    host = host.strip().lower()
    name, sep, port = host.rpartition(":")
    if sep and port == DEFAULT_PORTS.get(scheme) and "]" not in port:
        return name
    return host


def public_origin() -> tuple[str, str] | None:
    """The (scheme, host) of LLMS_TXT_PUBLIC_URL, or None when it is off or invalid."""
    raw = llms_txt_settings.llms_txt_public_url.strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        parts = None
    if (
        parts is None
        or parts.scheme not in DEFAULT_PORTS
        or not parts.hostname
        or parts.netloc.endswith(":")
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        logger.warning(
            "LLMS_TXT_PUBLIC_URL must be an origin such as https://my-app.com; llms.txt stays off"
        )
        return None
    return parts.scheme, _normalize_host(parts.scheme, parts.netloc)


def _calls(dependant: Dependant) -> Iterator[Callable[..., Any] | None]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def api_routes(app: FastAPI) -> list[Any]:
    """Every API route with its full path and all of its dependencies.

    FastAPI 0.16x keeps an included router nested in `app.routes`, so a plain
    loop over `app.routes` misses every route added with `include_router`.
    `iter_route_contexts` returns each route as FastAPI serves it: prefix
    applied, router-level dependencies included. Older FastAPI versions copy
    included routes into `app.routes` and have no `iter_route_contexts`.
    """
    iter_route_contexts = getattr(fastapi.routing, "iter_route_contexts", None)
    if iter_route_contexts is None:
        return [r for r in app.routes if isinstance(r, APIRoute)]
    return [c for c in iter_route_contexts(app.routes) if isinstance(c.original_route, APIRoute)]


def requires_auth(route: Any) -> bool:
    """True when the route, or any dependency under it, checks credentials.

    That means `validate_token`, or any FastAPI security scheme such as
    `HTTPBearer` or `OAuth2PasswordBearer` (a `SecurityBase` instance).
    A route whose dependencies cannot be read counts as protected (fail closed).
    """
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return True
    return any(
        call is validate_token or isinstance(call, SecurityBase) for call in _calls(dependant)
    )


def _has_protected_callback(route: Any) -> bool:
    """True when an OpenAPI callback on the route is protected or cannot be read.

    FastAPI copies callback operations and their models into the schema.
    """
    callbacks = getattr(route, "callbacks", None) or []
    return any(not isinstance(cb, APIRoute) or requires_auth(cb) for cb in callbacks)


def is_marked(route: Any) -> bool:
    return bool((getattr(route, "openapi_extra", None) or {}).get(MARKER))


def public_routes(app: FastAPI) -> list[Any]:
    """Marked routes that do not require sign-in, sorted by path.

    A route with `include_in_schema=False` is left out too: the public schema
    would not hold it, so its llms.txt link would point at nothing.
    """
    routes = []
    for route in api_routes(app):
        if not is_marked(route) or not getattr(route, "include_in_schema", True):
            continue
        if requires_auth(route) or _has_protected_callback(route):
            logger.warning("llms.txt skips %s: marked public but requires sign-in", route.path)
            continue
        routes.append(route)
    return sorted(routes, key=lambda r: r.path)


def _one_line(text: str | None) -> str:
    """Collapse whitespace so one value cannot break the Markdown list."""
    return " ".join((text or "").split())


def _summary(route: Any) -> str:
    if route.summary:
        return _one_line(route.summary)
    first = (route.description or "").strip().split("\n\n", 1)[0]
    return _one_line(first)


def _item(title: str, url: str, description: str) -> str:
    title = _one_line(title).replace("[", "(").replace("]", ")")
    return f"- [{title}]({url})" + (f": {description}" if description else "")


def valid_pages() -> list[PublicPage]:
    """PUBLIC_PAGES entries with a usable path. Others are left out with a warning."""
    pages = []
    for page in PUBLIC_PAGES:
        if PAGE_PATH_RE.match(page.path) and _one_line(page.title):
            pages.append(page)
        else:
            logger.warning("llms.txt skips page %r: bad path or empty title", page.path)
    return pages


def public_openapi(app: FastAPI) -> dict[str, Any]:
    """An OpenAPI document that holds the public routes only."""
    return get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=public_routes(app),
    )


def build_llms_txt(app: FastAPI, origin: str) -> str:
    """Render the llms.txt Markdown for `app`, with links under `origin`."""
    lines = [f"# {_one_line(app.title)}", ""]
    if description := _one_line(app.description):
        lines += [f"> {description}", ""]

    pages = valid_pages()
    if pages:
        lines += ["## Pages", ""]
        for page in pages:
            lines.append(_item(page.title, origin + page.path, _one_line(page.description)))
        lines.append("")

    # Each operation links to the public OpenAPI document, not to the endpoint
    # itself: a POST or a path with `{id}` cannot be opened with a plain GET.
    schema_url = origin + OPENAPI_PATH
    api = []
    for route in public_routes(app):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            api.append(_item(f"{method} {route.path}", schema_url, _summary(route)))
    if api:
        schema = _item("OpenAPI schema", schema_url, "Request and response shapes for these.")
        lines += ["## API", "", schema, *api, ""]

    return "\n".join(lines)


def _canonical_origin(request: Request) -> str:
    """The configured origin when this request came to its host; else raise 404."""
    origin = public_origin()
    if origin is None:
        raise HTTPException(status_code=404, detail="Not Found")
    scheme, host = origin
    if _normalize_host(scheme, request.headers.get("host", "")) != host:
        raise HTTPException(status_code=404, detail="Not Found")
    return f"{scheme}://{host}"


@router.api_route(PATH, methods=["GET", "HEAD"], include_in_schema=False)
async def llms_txt(request: Request) -> PlainTextResponse:
    """Serve llms.txt on the canonical host only; 404 everywhere else."""
    body = build_llms_txt(request.app, _canonical_origin(request))
    return PlainTextResponse(body, headers={"Cache-Control": CACHE_CONTROL})


@router.api_route(OPENAPI_PATH, methods=["GET", "HEAD"], include_in_schema=False)
async def llms_openapi(request: Request) -> JSONResponse:
    """Serve the public-only OpenAPI document under the same rules as llms.txt."""
    _canonical_origin(request)
    return JSONResponse(public_openapi(request.app), headers={"Cache-Control": CACHE_CONTROL})
