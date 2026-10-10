"""ObjectStoreSink behaviour that only shows in a fresh interpreter: exit-time puts and D-deps."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _run_child(
    script: str,
    *,
    env: dict[str, str] | None = None,
    pythonpath: Path | None = None,
    expect_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a fresh interpreter.

    If ``expect_success`` is True (default), fail the test if it exits non-zero.
    If False, return the result without checking the exit code.
    """
    # Start from the parent's env minus any store variable it may carry (the current name and the
    # 0.5.0 one); ``env`` sets them per test.
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("EVALSHIFT_CAPTURE_STORE", "EVALSHIFT_SINK")
    }
    child_env.update(env or {})
    if pythonpath is not None:
        existing = child_env.get("PYTHONPATH")
        child_env["PYTHONPATH"] = (
            f"{pythonpath}{os.pathsep}{existing}" if existing else str(pythonpath)
        )
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if expect_success:
        assert result.returncode == 0, result.stderr
    return result


def test_put_that_lazily_imports_a_thread_pool_survives_interpreter_shutdown(
    tmp_path: Path,
) -> None:
    # Regression: boto3's first ``import`` pulls in ``concurrent.futures.thread`` (via
    # s3transfer), which calls ``threading._register_atexit`` at import time. When the first put
    # only runs in the exit flush, interpreter shutdown has begun and that call raises
    # ``RuntimeError: can't register atexit after shutdown`` -- the capture is dropped. The helper
    # module stands in for the client library; the store waits until the main thread has
    # finished so the lazy import always lands inside the exit flush.
    (tmp_path / "lazy_client.py").write_text(
        "import concurrent.futures.thread  # noqa: F401 -- registers a threading atexit hook\n"
    )
    marker = tmp_path / "landed.json"
    _run_child(
        f"""
        import threading
        import time
        from pathlib import Path

        from evalshift.capture.span import SpanTree
        from evalshift.sinks.object_store import ObjectStoreSink
        from evalshift.trace.serialize import build_capture


        class LazyClientStore:
            uri = "lazy://bucket"

            def put(self, key: str, data: bytes) -> None:
                deadline = time.monotonic() + 10
                while threading.main_thread().is_alive() and time.monotonic() < deadline:
                    time.sleep(0.005)
                import lazy_client  # noqa: F401 -- first import happens during shutdown

                Path({str(marker)!r}).write_bytes(data)


        sink = ObjectStoreSink(LazyClientStore())
        sink.write(build_capture(SpanTree(), suite="s", agent_input="hi", capture_id="cap_1"))
        """,
        pythonpath=tmp_path,
    )
    assert marker.exists(), "the exit flush did not land the capture"
    assert json.loads(marker.read_text())["capture_id"] == "cap_1"


_CLOUD_MODULES = (
    "boto3",
    "botocore",
    "google.cloud.storage",
    "azure.storage.blob",
    "azure.identity",
)


@pytest.mark.parametrize("uri", ["s3://bucket/p", "gs://bucket/p", "az://acct/container/p"])
def test_import_with_env_sink_loads_no_cloud_client(uri: str) -> None:
    # D-deps: selecting a store must not import its client library; the client is built on the
    # first put. Namespace parents (``google``, ``azure``) may appear from ``find_spec``.
    result = _run_child(
        f"""
        import json
        import sys

        import evalshift
        from evalshift.config import active_sink

        sink = active_sink()
        sink = getattr(sink, "wrapped", sink)
        print(json.dumps({{
            "sink": type(sink).__name__,
            "loaded": [m for m in {list(_CLOUD_MODULES)!r} if m in sys.modules],
        }}))
        """,
        env={"EVALSHIFT_CAPTURE_STORE": uri},
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["sink"] == "ObjectStoreSink"
    assert report["loaded"] == []


def test_import_with_a_broken_sink_exits_zero_and_logs_nothing() -> None:
    # The CLI imports the SDK, and a developer may have both env vars exported in a shell:
    # `import evalshift` must stay silent and successful whatever EVALSHIFT_CAPTURE_STORE holds.
    result = _run_child(
        "import evalshift",
        env={"EVALSHIFT_CAPTURE_STORE": "ftp://bucket/prefix", "EVALSHIFT_CAPTURE": "1"},
    )
    assert "EVALSHIFT_CAPTURE_STORE" not in result.stderr


def test_decorating_with_a_broken_sink_fails_the_process_naming_the_fix() -> None:
    result = _run_child(
        "from evalshift import capture\ncapture.agent(suite='s', redact=False, tools=[])",
        env={"EVALSHIFT_CAPTURE_STORE": "ftp://bucket/prefix", "EVALSHIFT_CAPTURE": "1"},
        expect_success=False,
    )
    assert result.returncode != 0
    assert "SinkConfigurationError" in result.stderr
    assert "Accepted forms: s3://<bucket>/<prefix>" in result.stderr
    assert "ftp://bucket/prefix" not in result.stderr
