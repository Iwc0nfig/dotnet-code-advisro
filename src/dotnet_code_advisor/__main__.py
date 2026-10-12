"""``python -m dotnet_code_advisor`` -- the read-only MCP server.

Speaks stdio by default and streamable-http with ``--transport streamable-http``,
which is how it runs on the Linux server: one long-lived process on the LAN
rather than one subprocess per client.

Nothing here can write to the workspace: the tool layer registers no mutating
tool (enforced in tools/__init__.py), and the only writable path the process
ever touches is the index directory.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

from mcp.server import MCPServer

from .config import load_config
from .tools import register_all
from .tools.common import Ctx

log = logging.getLogger("dotnet-code-advisor")

INSTRUCTIONS = """Answer from the configured codebase, never from assumed project knowledge.
Read source before describing behavior. Cite file and stable symbol/member names.
Distinguish roslyn-index/roslyn compiler evidence, graph structural matches and filesystem text.
Report no_results with searched_scope; unavailable means a layer could not answer.
Report all caveats, truncation, ambiguity, dispatch, candidate and HTTP route-match evidence.
Pass compiler symbol keys on follow-ups. Mention projects with binding errors.
Cite markup for generated fields. Separate observed behavior from unverified inference.
Sides net48 and net8 identify configured legacy and modern roots, not namespaces.
The tools are read-only. Propose edits as grounded diffs; never claim to have applied them.
Call index_status when results are empty or surprising. No source-control history is queried.
"""



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dotnet-code-advisor")
    ap.add_argument("--config", default=None)
    ap.add_argument(
        "--no-roslyn",
        action="store_true",
        help="do not start the Roslyn hosts; graph and filesystem layers only",
    )
    ap.add_argument(
        "--list-tools", action="store_true", help="print the tool surface and exit"
    )
    ap.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="stdio for a locally spawned client; streamable-http to serve over the network",
    )
    ap.add_argument("--host", default="127.0.0.1", help="bind address for --transport streamable-http")
    ap.add_argument("--port", type=int, default=8400, help="bind port for --transport streamable-http")
    ap.add_argument("--path", default="/mcp", help="HTTP path the MCP endpoint is served on")
    ap.add_argument(
        "--stateless-http",
        action="store_true",
        help="do not keep per-client session state between requests",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # stdout is the MCP transport
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    ctx = Ctx.create(cfg)
    ctx.pool.disabled = args.no_roslyn

    mcp = MCPServer(
        "dotnet-code-advisor",
        title="Dotnet Code Advisor Code Advisor",
        version="0.2.0",
        instructions=INSTRUCTIONS,
    )
    names = register_all(mcp, ctx)
    log.info("registered %d read-only tools: %s", len(names), ", ".join(sorted(names)))

    if args.list_tools:
        for n in sorted(names):
            print(n)
        return 0

    if not args.no_roslyn:
        # Load in the background: the solutions take seconds to tens of seconds,
        # and until a host is ready its tools answer "unavailable" rather than
        # silently degrading to a weaker layer.
        def warm() -> None:
            try:
                states = ctx.pool.start_all(load=True)
                for side, st in states.items():
                    log.info(
                        "roslyn %s: %s (%s/%s projects, %s website docs, %s failures)",
                        side, st.state, st.projects_loaded, st.projects_total,
                        st.website_documents, st.failure_count,
                    )
            except Exception:
                log.exception("roslyn warm-up failed; compiler-exact tools stay unavailable")

        threading.Thread(target=warm, name="roslyn-warmup", daemon=True).start()

    try:
        if args.transport == "streamable-http":
            # DNS-rebinding protection is off unless TransportSecuritySettings is
            # passed, so binding 0.0.0.0 and connecting by LAN IP or hostname works
            # without Host-header configuration.  If this box ever becomes reachable
            # more widely, that is the setting to add -- see README.
            log.info(
                "serving MCP over streamable-http at http://%s:%d%s",
                args.host, args.port, args.path,
            )
            mcp.run(
                "streamable-http",
                host=args.host,
                port=args.port,
                streamable_http_path=args.path,
                stateless_http=args.stateless_http,
            )
        else:
            mcp.run()
    finally:
        ctx.pool.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
