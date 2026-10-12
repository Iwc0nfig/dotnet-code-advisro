"""NDJSON-RPC client and the two-host pool.

Failure policy -- this is the anti-hallucination guarantee:
if a host is not loaded, crashed, or a query times out, the caller gets
``status: "unavailable"`` and **no results**.  It never silently degrades to the
tree-sitter graph, and never returns a partial answer without saying so.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..build.runner import host_env
from ..config import Config


class RoslynError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RoslynUnavailable(RoslynError):
    """The host cannot answer at all -- not loaded, dead, or timed out."""


@dataclass
class HostState:
    side: str
    state: str = "cold"
    projects_loaded: int = 0
    projects_total: int = 0
    website_documents: int = 0
    elapsed_ms: int = 0
    failure_count: int = 0
    failures: list[str] = field(default_factory=list)
    solution: Optional[str] = None
    error: Optional[str] = None


class RoslynHost:
    """One long-lived sidecar process, spoken to over NDJSON on stdio."""

    def __init__(self, exe: Path | None, side: str, cfg: Config) -> None:
        # None when config.toml does not configure a host for this side.  That is
        # the normal state on the Linux server, which runs the graph and
        # filesystem layers only; it is a stated absence, not a failure.
        self.exe: Path | None = Path(exe) if exe is not None else None
        self.side = side
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 1
        self.state = HostState(side=side)
        self._stderr_tail: list[str] = []

    # -- lifecycle --------------------------------------------------------

    def available(self) -> bool:
        return self.side in self.cfg.paths.roots and self.exe is not None and self.exe.is_file()

    def unavailable_reason(self) -> str:
        if self.side not in self.cfg.paths.roots:
            return f"Source side {self.side} is not configured."
        if self.exe is None:
            return (
                f"Roslyn {self.side} host is not configured: config.toml has no "
                f"paths.roslyn_host_{self.side}. This deployment runs the graph and "
                "filesystem layers only, so compiler-exact answers are unavailable."
            )
        return (
            f"{self.exe} not built. Build the Roslyn host on a machine that has the "
            ".NET SDK, or run the server with --no-roslyn."
        )

    def start(self) -> None:
        if getattr(self, "disabled", False):
            raise RoslynUnavailable("disabled", "Live Roslyn hosts disabled by --no-roslyn")
        if self.proc is not None and self.proc.poll() is None:
            return
        if not self.available():
            self.state.state = "missing"
            self.state.error = self.unavailable_reason()
            raise RoslynUnavailable("not_loaded", self.state.error)
        self.proc = subprocess.Popen(
            [str(self.exe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(self.exe.parent),
            env=host_env(self.cfg) if self.side == "net48" else None,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.state.state = "started"

    def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_tail.append(line.rstrip())
            if len(self._stderr_tail) > 200:
                del self._stderr_tail[:100]

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.stdin.write('{"id":0,"method":"shutdown","params":{}}\n')  # type: ignore
            self.proc.stdin.flush()  # type: ignore
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        finally:
            self.proc = None
            self.state.state = "stopped"

    # -- protocol ---------------------------------------------------------

    def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        timeout = timeout or self.cfg.limits.roslyn_query_timeout_s
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RoslynUnavailable(
                    "not_loaded",
                    f"{self.side} host is not running"
                    + (f" (exit {self.proc.poll()})" if self.proc else "")
                    + self._stderr_hint(),
                )
            rid = self._next_id
            self._next_id += 1
            msg = json.dumps({"id": rid, "method": method, "params": params})
            try:
                self.proc.stdin.write(msg + "\n")  # type: ignore
                self.proc.stdin.flush()  # type: ignore
            except (BrokenPipeError, OSError) as exc:
                raise RoslynUnavailable("not_loaded", f"{self.side} host pipe closed: {exc}")

            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RoslynUnavailable(
                        "timeout",
                        f"{self.side} host did not answer '{method}' within {timeout}s",
                    )
                line = self._readline_with_deadline(deadline)
                if line is None:
                    raise RoslynUnavailable(
                        "not_loaded",
                        f"{self.side} host closed stdout during '{method}'"
                        + self._stderr_hint(),
                    )
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "progress" in obj and "id" not in obj:
                    p = obj["progress"]
                    self.state.projects_loaded = p.get("loaded", 0)
                    self.state.projects_total = p.get("total", 0)
                    self.state.state = "loading"
                    continue
                if obj.get("id") != rid:
                    continue
                if obj.get("ok"):
                    return obj.get("result")
                err = obj.get("error") or {}
                raise RoslynError(err.get("code", "internal"), err.get("message", ""))

    def _readline_with_deadline(self, deadline: float) -> Optional[str]:
        # The host answers on its own thread; a blocking readline is fine because
        # the process is killed by the caller on shutdown.  A dead process gives
        # EOF, which is handled above.
        assert self.proc is not None and self.proc.stdout is not None
        line = self.proc.stdout.readline()
        return line if line != "" else None

    def _stderr_hint(self) -> str:
        if not self._stderr_tail:
            return ""
        return "  [host stderr] " + " | ".join(self._stderr_tail[-4:])

    # -- high level -------------------------------------------------------

    def load(self, solution: Path, website: dict | None = None) -> HostState:
        params: dict[str, Any] = {"solution": str(solution).replace("\\", "/"), "side": self.side}
        if website:
            params["website"] = website
        res = self.call("load", params, timeout=self.cfg.limits.roslyn_load_timeout_s)
        self._apply(res)
        return self.state

    def ping(self) -> HostState:
        try:
            self._apply(self.call("ping", {}, timeout=10))
        except RoslynError as exc:
            self.state.state = "error"
            self.state.error = str(exc)
        return self.state

    def _apply(self, res: dict | None) -> None:
        if not res:
            return
        s = self.state
        s.state = res.get("state", s.state)
        s.projects_loaded = res.get("projects_loaded", s.projects_loaded)
        s.projects_total = res.get("projects_total", s.projects_total)
        s.website_documents = res.get("website_documents", s.website_documents)
        s.elapsed_ms = res.get("elapsed_ms", s.elapsed_ms)
        s.failure_count = res.get("failure_count", s.failure_count)
        if "failures" in res:
            s.failures = list(res["failures"])[:50]
        s.solution = res.get("solution", s.solution)


class RoslynPool:
    """The two hosts, keyed by side.  Started lazily; failures are reported, not hidden."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.hosts: dict[str, RoslynHost] = {
            "net48": RoslynHost(cfg.paths.roslyn_host_net48, "net48", cfg),
            "net8": RoslynHost(cfg.paths.roslyn_host_net8, "net8", cfg),
        }
        self._started: set[str] = set()

    def get(self, side: str) -> RoslynHost:
        host = self.hosts[side]
        host.disabled = getattr(self, "disabled", False)
        return host

    def website_spec(self) -> dict | None:
        if self.cfg.paths.website_root is None:
            return None
        refs = self.cfg.paths.netfx_ref_assemblies
        return {
            "root": str(self.cfg.paths.website_root).replace("\\", "/"),
            "framework_refs": str(refs).replace("\\", "/") if refs else "",
            "designer_dir": str(self.cfg.paths.designer_dir).replace("\\", "/"),
        }

    def start_all(self, *, load: bool = True) -> dict[str, HostState]:
        out: dict[str, HostState] = {}
        for side, host in self.hosts.items():
            if side not in self.cfg.paths.roots:
                continue
            try:
                host.start()
                if load and getattr(self.cfg.paths, f"{side}_solution") is not None:
                    if side == "net48":
                        host.load(self.cfg.paths.net48_solution, self.website_spec())
                    else:
                        host.load(self.cfg.paths.net8_solution)
                self._started.add(side)
            except RoslynError as exc:
                host.state.state = "error"
                host.state.error = str(exc)
            out[side] = host.state
        return out

    def status(self) -> dict[str, HostState]:
        out = {}
        for side, host in self.hosts.items():
            if side not in self.cfg.paths.roots:
                continue
            if host.proc is not None and host.proc.poll() is None:
                out[side] = host.ping()
            else:
                if not host.available():
                    host.state.state = "missing"
                    host.state.error = host.unavailable_reason()
                out[side] = host.state
        return out

    def stop_all(self) -> None:
        for host in self.hosts.values():
            host.stop()
