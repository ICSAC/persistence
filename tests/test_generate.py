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
import re
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


SEEN: list[tuple[str, str]] = []   # (method, path) of every request to /papers/


class Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):  # noqa: N802
        SEEN.append(("HEAD", self.path))
        if self.path in ("/papers/111-icsac.pdf", "/papers/ICSAC-SUB-00042-icsac.pdf", "/papers/ICSAC-SUB-00043.pdf"):
            self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.end_headers()
        elif self.path == "/papers/222-icsac.pdf":   # an app shell answering 200 for a missing file
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/papers/"):
            SEEN.append(("GET", self.path))
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

            # 1b. volume and order follow the publication date; "accepted" stays the acceptance
            pub_rows = generate.build_rows([
                {"slug": "late", "title": "Late", "doi": "10.67697/icsac.2027.002",
                 "accepted_date": "2027-05-30", "published_date": "2027-06-02"},
                {"slug": "early", "title": "Early", "doi": "10.67697/icsac.2027.003", "accepted_date": "2027-05-31"},
            ], generate.parse_volumes(VOLUMES_TS), check=False)
            check([r["article_url"].rsplit("/", 1)[1] for r in pub_rows] == ["early", "late"],
                  "rows in order of publication")
            check(pub_rows[1]["volume"] == "2" and pub_rows[1]["accepted"] == "2027-05-30",
                  "an article accepted before a volume opens but published after it is in the new volume")

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

    pdf_column()
    print(f"{PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


def pdf_column() -> None:
    """The PDF column: the page's rule picks the file, one HEAD checks it."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cc_by = "https://creativecommons.org/licenses/by/4.0/"
    reg_rows = [
        {"slug": "zenodo-ok", "title": "Zenodo route", "doi": "10.5281/zenodo.111", "accepted_date": "2026-04-19",
         "record_id": "111"},
        {"slug": "zenodo-html", "title": "Answers HTML", "doi": "10.5281/zenodo.222", "accepted_date": "2026-04-19",
         "record_id": "222"},
        {"slug": "covered", "title": "Covered", "doi": "10.5555/icsac.2026.042", "accepted_date": "2026-09-29",
         "sub_id": "ICSAC-SUB-00042", "license_url": cc_by},
        {"slug": "bare-only", "title": "Bare only", "doi": "10.5555/icsac.2026.043", "accepted_date": "2026-09-30",
         "sub_id": "ICSAC-SUB-00043", "license_url": cc_by},
        {"slug": "missing", "title": "Missing", "doi": "10.5555/icsac.2026.044", "accepted_date": "2026-10-01",
         "sub_id": "ICSAC-SUB-00044", "license_url": cc_by},
        {"slug": "doi-only", "title": "DOI only", "doi": "10.5555/x.1", "accepted_date": "2026-10-02",
         "license_url": cc_by},
    ]
    try:
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            site = t / "site"
            (site / "public/papers").mkdir(parents=True)
            (site / "public/papers/ICSAC-SUB-00042-icsac.pdf").write_bytes(b"%PDF-1.7")
            reg = t / "accepted.json"; reg.write_text(json.dumps(reg_rows))
            vol = t / "persistence.ts"; vol.write_text(VOLUMES_TS)
            out = t / "out"
            SEEN.clear()
            code, log = run(["--registry", str(reg), "--volumes", str(vol), "--site-repo", str(site),
                             "--out", str(out), "--no-crossref", "--pdf-base", base])
            rows = {r["article_url"].rsplit("/", 1)[1]: r for r in json.loads((out / "index.json").read_text())["articles"]}
            check(len(rows) == 6, "pdf: every row kept")
            check(rows["covered"]["pdf_url"] == f"{base}/papers/ICSAC-SUB-00042-icsac.pdf"
                  and rows["covered"]["pdf_edition"] == "ICSAC edition", "pdf: the covered edition is preferred when it exists")
            check(rows["bare-only"]["pdf_url"] == f"{base}/papers/ICSAC-SUB-00043.pdf"
                  and rows["bare-only"]["pdf_edition"] == "author's original", "pdf: the bare file is the fallback")
            check(rows["zenodo-ok"]["pdf_url"].endswith("/papers/111-icsac.pdf"), "pdf: a Zenodo-route paper links its ICSAC edition")
            check(rows["covered"]["pdf_status"] == "reachable" and rows["bare-only"]["pdf_status"] == "reachable",
                  "pdf: 200 + application/pdf reads reachable")
            check(rows["missing"]["pdf_status"] == "couldn't reach (HTTP 404)", "pdf: a missing file reads couldn't reach (HTTP 404)")
            check(rows["zenodo-html"]["pdf_status"] == "couldn't reach (not a PDF: text/html)",
                  "pdf: a 200 that isn't a PDF reads couldn't reach (not a PDF)")
            check(rows["doi-only"]["pdf_url"] is None and rows["doi-only"]["pdf_status"] is None,
                  "pdf: no file on the site, no link and no request")
            check(code == 1 and "PDF couldn't reach (HTTP 404)" in log and "not a PDF" in log,
                  "pdf: an unreachable PDF makes the run exit 1 and is named")
            check(all(m == "HEAD" for m, _ in SEEN) and len(SEEN) == 5 and len(set(SEEN)) == 5,
                  f"pdf: one HEAD per PDF, never a GET ({SEEN})")
            readme = (out / "README.md").read_text()
            check("| PDF |" in readme and "[PDF](<" in readme, "pdf: README has a PDF column with links")
            check("PDF (author's original)" in readme, "pdf: a fallback row says it's the author's original")
            check("Couldn't reach (not a PDF: text/html)" in readme, "pdf: README shows the failed check")
            check("No PDF on the site" in readme, "pdf: README says when there is no PDF")
            check("CC0 1.0" in readme and "published under CC BY 4.0, as each article's PDF and DOI record state" in readme,
                  "licence: CC0 for the index; the articles CC BY 4.0 when every one is on record as CC BY 4.0")
            check(readme.count("\n| [") == 6, "pdf: six table rows")
            csv_head = (out / "index.csv").read_text().splitlines()[0]
            check("pdf_url,pdf_edition,pdf_status" in csv_head, "pdf: CSV carries the PDF fields")

            reg.write_text(json.dumps(reg_rows[:2] + [dict(reg_rows[2], license_url="https://example.com/own")]))
            code, _ = run(["--registry", str(reg), "--volumes", str(vol), "--site-repo", str(site),
                           "--out", str(t / "out2"), "--no-check", "--no-crossref"])
            check("under the licence its PDF and DOI record state" in (t / "out2/README.md").read_text(),
                  "licence: any other licence on record -> the README points at each article's own statement")

            # git mode looks for the covered file in the tree at the ref, not the working copy
            repo = t / "gitsite"
            (repo / "src/data").mkdir(parents=True); (repo / "src/lib").mkdir(parents=True)
            (repo / "public/papers").mkdir(parents=True)
            (repo / "src/data/accepted.json").write_text(json.dumps(reg_rows[2:4]))
            (repo / "src/lib/persistence.ts").write_text(VOLUMES_TS)
            (repo / "public/papers/ICSAC-SUB-00042-icsac.pdf").write_bytes(b"%PDF-1.7")
            g = ["git", "-C", str(repo), "-c", "user.email=test@example.com", "-c", "user.name=test"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(g + ["add", "-A"], check=True)
            subprocess.run(g + ["commit", "-q", "-m", "fixture"], check=True)
            (repo / "public/papers/ICSAC-SUB-00043-icsac.pdf").write_bytes(b"%PDF-1.7")   # not committed
            code, _ = run(["--site-repo", str(repo), "--ref", "HEAD", "--out", str(t / "out3"), "--no-check", "--no-crossref"])
            rows = {r["article_url"].rsplit("/", 1)[1]: r for r in json.loads((t / "out3/index.json").read_text())["articles"]}
            check(rows["covered"]["pdf_url"] == "https://icsacinstitute.org/papers/ICSAC-SUB-00042-icsac.pdf",
                  "git mode: a committed covered file is linked")
            check(rows["bare-only"]["pdf_url"] == "https://icsacinstitute.org/papers/ICSAC-SUB-00043.pdf",
                  "git mode: an uncommitted covered file is not")
            check(rows["covered"]["pdf_status"] == "not checked", "--no-check: PDFs read not checked")
            # COUNTER-Robots patterns "bot" and "link.?check": a check must never read as a download
            check(re.search(r"bot", generate.USER_AGENT, re.I) and re.search(r"link.?check", generate.USER_AGENT, re.I),
                  "the checker's user agent is one the site's usage counter files as a robot")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
