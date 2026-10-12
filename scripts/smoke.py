"""Read-only smoke check against whichever workspace the explicit config selects."""
import argparse
from mcp.server import MCPServer
from dotnet_code_advisor.config import load_config
from dotnet_code_advisor.tools import register_all
from dotnet_code_advisor.tools.common import Ctx

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    args = parser.parse_args()
    ctx = Ctx.create(load_config(args.config))
    ctx.pool.disabled = True
    server = MCPServer("smoke")
    names = register_all(server, ctx)
    try:
        status = server._tool_manager.get_tool("index_status").fn()
        print(f"{len(names)} read-only tools; {status.files_indexed} indexed files.")
        if status.files_indexed == 0:
            raise SystemExit("Index is empty; inspect configuration and exclusions.")
        print("Graph and filesystem layers ready. Check index_status for compiler coverage.")
    finally:
        ctx.pool.stop_all()
        ctx.con.close()

if __name__ == "__main__":
    main()
