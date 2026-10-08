"""JSON-endpoints til forsiden på den hostede server. Ingen model, kun de rene tool-funktioner.

  GET /api/tools/<navn>?area=DK2&start=...&end=...   samme svar som MCP-tool'et giver
  GET /api/status                                    datadækning, kilde og grænser

Adgangsprincip:
  - Kun de fem read-tools fra tools.READ_TOOLS. Write-tool'et kan ikke kaldes herfra.
  - Parametre valideres mod samme skema som modellen ser (ukendte parametre afvises),
    og derefter af de samme pydantic-modeller og grænser som i MCP-serveren.
  - Fejl går tilbage som {"error": "..."} med status 400, aldrig som stack trace.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from ladeagent import config, tools
from ladeagent.data.eds import DataStore

CACHE = {"Cache-Control": "public, max-age=300"}  # priserne ændrer sig højst én gang i døgnet
NO_CACHE = {"Cache-Control": "no-store"}


def parse_args(name: str, query: dict[str, str]) -> dict:
    """Query-parametre -> argumenter til tool'et, valideret mod tool'ets eget skema."""
    schema = tools.READ_TOOLS[name]["schema"]
    props = schema["properties"]
    unknown = sorted(set(query) - set(props))
    if unknown:
        raise tools.ToolError(f"Ukendt parameter: {', '.join(unknown)}. Tilladt: {', '.join(props)}.")
    missing = [k for k in schema["required"] if not query.get(k)]
    if missing:
        raise tools.ToolError(f"Mangler parameter: {', '.join(missing)}.")
    args: dict = {}
    for key, raw in query.items():
        if props[key]["type"] == "number":
            try:
                value = float(raw.replace(",", "."))
            except ValueError as e:
                raise tools.ToolError(f"{key} skal være et tal. Fik: {raw!r}") from e
            if not math.isfinite(value):
                raise tools.ToolError(f"{key} skal være et tal. Fik: {raw!r}")
            args[key] = value
        else:
            args[key] = raw
    return args


def call_tool(store: DataStore, name: str, query: dict[str, str]) -> tuple[int, dict]:
    """Returnerer (statuskode, svar). Ren funktion, så den kan testes uden server."""
    if name not in tools.READ_TOOLS:
        return 404, {"error": f"Ukendt tool {name!r}. Kun read-tools kan kaldes her: {', '.join(tools.READ_TOOLS)}."}
    try:
        return 200, tools.READ_TOOLS[name]["fn"](store, **parse_args(name, query))
    except tools.ToolError as e:
        return 400, {"error": str(e)}


def status(store: DataStore) -> dict:
    return {
        "source": store.source,
        "coverage": store.coverage(),
        "areas": config.AREAS,
        "limits": {"max_window_days": config.MAX_WINDOW_DAYS, "max_kwh": config.MAX_KWH, "max_kw": config.MAX_KW},
        "read_tools": list(tools.READ_TOOLS),
    }


def register(mcp, get_store: Callable[[], DataStore], site_dir: Path | None = None) -> None:
    """Hænger endpoints på MCP-serverens HTTP-app. `site_dir` bruges kun lokalt; i drift serverer nginx siden."""

    @mcp.custom_route("/api/tools/{name}", methods=["GET"])
    async def api_tool(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            store = await run_in_threadpool(get_store)
        except Exception:  # noqa: BLE001  -- datakilden svarer ikke, og der er intet at falde tilbage på
            return JSONResponse({"error": "Data er ikke tilgængelige lige nu. Prøv igen om lidt."}, status_code=503, headers=NO_CACHE)
        code, body = await run_in_threadpool(call_tool, store, name, dict(request.query_params))
        return JSONResponse(body, status_code=code, headers=CACHE if code == 200 else NO_CACHE)

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> Response:
        try:
            store = await run_in_threadpool(get_store)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Data er ikke tilgængelige lige nu. Prøv igen om lidt."}, status_code=503, headers=NO_CACHE)
        return JSONResponse(status(store), headers=CACHE)

    if site_dir is None:
        return
    root = site_dir.resolve()

    @mcp.custom_route("/", methods=["GET"])
    async def site_index(request: Request) -> Response:
        return FileResponse(root / "index.html", headers=NO_CACHE)

    @mcp.custom_route("/quality.json", methods=["GET"])
    async def site_quality(request: Request) -> Response:
        # Runde 2-tallene til forsiden. I drift kopierer deploy/install.sh filen til /var/www; lokalt læses den herfra.
        target = config.ROOT / "evals" / "reports" / "quality.json"
        if not target.is_file():
            return JSONResponse({"error": "quality.json findes ikke endnu. Kør make quality-all."}, status_code=404, headers=NO_CACHE)
        return FileResponse(target, headers=NO_CACHE, media_type="application/json")

    @mcp.custom_route("/assets/{path:path}", methods=["GET"])
    async def site_asset(request: Request) -> Response:
        target = (root / "assets" / request.path_params["path"]).resolve()
        if not target.is_file() or not target.is_relative_to(root / "assets"):
            return Response(status_code=404)
        return FileResponse(target, headers=NO_CACHE)
