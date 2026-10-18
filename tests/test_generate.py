#!/usr/bin/env python3
"""Checks for scripts/generate.py. Runs offline: links point at a local test
server, the registry and volume table are fixtures (one run reads them through
a throwaway git repository). Scratch directories are removed pass or fail.

    python3 tests/test_generate.py
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import generate  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(cond: bool, label: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(label)
        print(f"FAIL: {label}")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/ok":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        elif self.path == "/moved":
            self.send_response(302); self.send_header("Location", "/ok"); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


VOLUMES_TS = """export const PERSISTENCE_VOLUMES: ReadonlyArray<{ volume: string; from: string; year: string }> = [
  { volume: '1', from: '2026-01-01', year: '2026' },
  { volume: '2', from: '2027-06-01', year: '2027' },
];
"""


def registry(base: str) -> list[dict]:
    return [
        {"slug": "no-link", "title": "A paper | with a pipe", "doi": "10.5281/zenodo.1", "accepted_date": "2026-04-19"},
        {"slug": "good-link", "title": "Good link", "doi": "10.67697/icsac.2026.001", "accepted_date": "2026-09-29",
         "code_data_url": f"{base}/moved"},
        {"slug": "dead-link", "title": "Dead link", "doi": "doi:10.67697/icsac.2027.001", "accepted_date": "2027-07-01",
         "code_data_url": f"{base}/gone"},
    ]


def run(args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = generate.main(args)
    return code, buf.getvalue()


def main() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            reg = t / "accepted.json"; reg.write_text(json.dumps(registry(base)))
            vol = t / "persistence.ts"; vol.write_text(VOLUMES_TS)
            out = t / "out"
            common = ["--registry", str(reg), "--volumes", str(vol), "--out", str(out), "--no-crossref"]

            # 1. every row kept; a dead link is marked, not dropped; exit 1 asks for a look
            code, log = run(common + ["--today", "2026-10-17"])
            doc = json.loads((out / "index.json").read_text())
            rows = doc["articles"]
            check(code == 1, "a dead link makes the run exit 1")
            check(len(rows) == 3, "all three registry rows are in the index")
            by = {r["article_url"].rsplit("/", 1)[1]: r for r in rows}
            check(by["dead-link"]["link_status"] == "couldn't reach (HTTP 404)", "the 404 link reads couldn't reach (HTTP 404)")
            check(by["good-link"]["link_status"] == "reachable", "a redirect to a 200 reads reachable")
            check(by["no-link"]["code_data_url"] is None and by["no-link"]["link_status"] is None, "no link: no status")
            check(by["dead-link"]["doi"] == "10.67697/icsac.2027.001", "doi: prefix is stripped")
            check(by["dead-link"]["volume"] == "2" and by["good-link"]["volume"] == "1", "volumes follow the table")
            check(by["good-link"]["hosted_by"] == "Author", "an outside link is hosted by the author")
            check("NEEDS A LOOK" in log and "/gone" in log, "the dead link is named in the run output")
            readme = (out / "README.md").read_text()
            check("Couldn't reach (HTTP 404)" in readme, "README shows Couldn't reach")
            check("A paper \\| with a pipe" in readme, "a pipe in a title is escaped in the table")
            check("No link on record" in readme, "README says No link on record")
            check(readme.index("## Volume 2") < readme.index("## Volume 1"), "newest volume first, as on /journal")
            check(readme.count("\n| [") == 3, "three table rows in the README")
            csv_lines = (out / "index.csv").read_text().strip().splitlines()
            check(len(csv_lines) == 4, "CSV has a header and three rows")
            check(doc["updated"] == "2026-10-17", "first build stamps today")

            # 2. unchanged rows keep the old date; a changed row moves it
            code, _ = run(common + ["--today", "2026-10-20"])
            check(json.loads((out / "index.json").read_text())["updated"] == "2026-10-17", "unchanged rows keep the last-changed date")
            reg.write_text(json.dumps(registry(base)[:2] + [dict(registry(base)[2], code_data_url=f"{base}/ok")]))
            code, _ = run(common + ["--today", "2026-10-21"])
            doc = json.loads((out / "index.json").read_text())
            check(code == 0, "all links reachable: exit 0")
            check(doc["updated"] == "2026-10-21", "a changed row moves the last-changed date")

            # 3. refuse to build, write nothing
            before = (out / "README.md").read_text()
            bad = t / "bad.json"; bad.write_text(json.dumps([{"slug": "x", "title": "t", "doi": ""}]))
            code, log = run(["--registry", str(bad), "--volumes", str(vol), "--out", str(out), "--no-check", "--no-crossref"])
            check(code == 2 and "missing doi, accepted_date" in log, "a row missing fields stops the build and names them")
            check((out / "README.md").read_text() == before, "a refused build writes nothing")
            novol = t / "novol.ts"; novol.write_text("export const X = 1;\n")
            code, log = run(["--registry", str(reg), "--volumes", str(novol), "--out", str(out), "--no-check", "--no-crossref"])
            check(code == 2 and "volume table" in log, "a missing volume table stops the build")
            code, log = run(["--site-repo", str(t / "nope"), "--out", str(out), "--no-check", "--no-crossref"])
            check(code == 2 and "couldn't read" in log, "an unreadable site repository stops the build")

            # 4. reads the registry through git at a ref, not the working tree
            repo = t / "site"
            (repo / "src/data").mkdir(parents=True); (repo / "src/lib").mkdir(parents=True)
            (repo / "src/data/accepted.json").write_text(json.dumps(registry(base)[:1]))
            (repo / "src/lib/persistence.ts").write_text(VOLUMES_TS)
            g = ["git", "-C", str(repo), "-c", "user.email=test@example.com", "-c", "user.name=test"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(g + ["add", "-A"], check=True)
            subprocess.run(g + ["commit", "-q", "-m", "fixture"], check=True)
            (repo / "src/data/accepted.json").write_text(json.dumps(registry(base)))  # uncommitted edit
            out2 = t / "out2"
            code, _ = run(["--site-repo", str(repo), "--ref", "HEAD", "--out", str(out2), "--no-check", "--no-crossref"])
            rows = json.loads((out2 / "index.json").read_text())["articles"]
            check(code == 0 and len(rows) == 1, "git mode reads the committed registry, not the uncommitted edit")

            # 5. helpers
            check(generate.host_label("https://doi.org/10.5281/zenodo.23042786") == "Zenodo", "Zenodo DOI prefix")
            check(generate.host_label("https://github.com/someone/repo") == "GitHub (someone)", "GitHub owner shown")
            check(generate.host_label("https://www.osf.io/abcde/") == "OSF", "www. stripped")
            check(generate.host_label("https://doi.org/10.9999/x") == "DOI 10.9999", "unknown DOI prefix named, not guessed")
            check(generate.in_icsac_namespace("https://github.com/ICSAC/internal-dynamics-of-decisions"), "ICSAC GitHub user flagged")
            check(generate.in_icsac_namespace("https://huggingface.co/datasets/ICSAC-Institute/x"), "ICSAC HF org flagged")
            check(not generate.in_icsac_namespace("https://github.com/icsac-fan/x"), "a lookalike owner is not flagged")
            code, log = run(["--registry", str(reg), "--volumes", str(vol), "--out", str(t / "out3"), "--no-check", "--no-crossref"])
            check(code == 0 and "not checked" in (t / "out3/index.json").read_text(), "--no-check marks rows not checked, exit 0")
    finally:
        srv.shutdown()

    print(f"{PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
