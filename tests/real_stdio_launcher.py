"""Launcher for the G63 real-stdio E2E test.

Points config.UPSTREAM_COMMAND at fake_upstream.py (no uvx/Unity needed) and then runs
the real proxy.main() unmodified, so the test exercises main()'s actual sys.stdin/
sys.stdout setup — the only place a Windows-codepage bug can live. The in-process
Proxy() tests construct their own client_out object and never touch real stdio, so they
cannot see this class of bug.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vrc_mcp_proxy import config  # noqa: E402

config.UPSTREAM_COMMAND = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_upstream.py")]

from vrc_mcp_proxy.proxy import main  # noqa: E402

if __name__ == "__main__":
    main()
