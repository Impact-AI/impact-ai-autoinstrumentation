# SPDX-License-Identifier: Apache-2.0

"""Which workloads the overhead is measured on: every runtime and server the images claim, and a workload of its own
for each library that joined the traced set with cells of its own, so that the budget is measured with that library
imported, instrumented and called, and not only with OpenAI."""

from __future__ import annotations

import http.server
import sys
import threading
from pathlib import Path

import harness
from combos import all_combos

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import qualify_python as qualify  # noqa: E402


def own_fixture_libraries() -> dict[str, set[str]]:
    """library -> the requests of its cells, for each library whose cells run in a fixture named after it."""
    found: dict[str, set[str]] = {}
    for variant in qualify.VARIANTS.values():
        if variant.fixture == variant.library:
            found.setdefault(variant.library, set()).update(variant.requests)
    return found


def test_each_library_with_cells_of_its_own_has_a_workload_that_calls_it():
    libraries = own_fixture_libraries()
    assert "google-genai" in libraries
    for library, requests in libraries.items():
        workloads = [c for c in all_combos() if c.library == library]
        assert workloads, f"no overhead workload calls {library}"
        for combo in workloads:
            assert combo.fixture == library
            assert set(combo.routes) <= requests, f"{combo.id} asks for what the cells of {library} do not"
            assert combo.command[1] == f"{qualify.VARIANTS[f'{library}-2'].app}:app"


def test_the_google_genai_workload_runs_in_its_cells_fixture_and_calls_both_apis_every_way():
    (combo,) = [c for c in all_combos() if c.library == "google-genai"]
    assert harness.Setup(combo).workload.endswith("/fixture-app-google-genai:3.12")
    assert {route.split("/")[3] for route in combo.routes} == {"gemini", "vertex"}
    assert {route.split("/")[4] for route in combo.routes} == {"sync", "stream", "async", "async-stream"}


def test_the_openai_workloads_keep_their_image_and_call():
    for combo in all_combos():
        if combo.library is None:
            assert combo.routes == ("/chat/openai",)
            assert combo.fixture == ""
            if combo.runtime == "python":
                assert harness.Setup(combo).workload.endswith(f"/fixture-app:{combo.version}")


def test_ids_are_unique():
    ids = [c.id for c in all_combos()]
    assert len(ids) == len(set(ids))


def test_the_calls_take_the_workloads_routes_in_turn():
    asked: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - the name http.server calls
            self.rfile.read(int(self.headers["Content-Length"]))
            asked.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        class Workload:
            name = "workload"
            port = server.server_address[1]

        harness.drive(Workload(), 5, ("/a", "/b"))
    finally:
        server.shutdown()
        server.server_close()
    assert asked == ["/a", "/b", "/a", "/b", "/a"]
