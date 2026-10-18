#!/usr/bin/env python3
"""Build the Persistence code and data index.

One row per Persistence article: its DOI, its title, the link to the author's
code and data, and where that link is hosted. Links only: nothing here copies,
mirrors or stores anyone's files.

Source of truth: the journal's article registry (src/data/accepted.json in the
website repository), read at a committed ref (default origin/main) so the index
shows what the site has published. Each article page prints its code and data
link from the same field. Volumes come from src/lib/persistence.ts in the same
repository, so an article lands in the volume its page shows.

Writes README.md, index.json and index.csv into --out. A link that can't be
reached is marked "Couldn't reach" and keeps its row; no row is ever dropped.

Exit codes:
  0  built, every link reachable (or not checked)
  1  built, but something needs a look: a link couldn't be reached, a link
     points into an ICSAC namespace, or Crossref disagrees with the registry
  2  couldn't build: registry or volume table unreadable, a registry row is
     missing a required field, or the fetch failed (nothing is written)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REGISTRY_PATH = "src/data/accepted.json"
VOLUMES_PATH = "src/lib/persistence.ts"
SITE = "https://icsacinstitute.org"
USER_AGENT = "ICSAC-persistence-code-index/1.0 (+https://icsacinstitute.org; mailto:info@icsacinstitute.org)"
TIMEOUT = 20
REQUIRED = ("slug", "title", "doi", "accepted_date")

# Final host after redirects -> the name the table shows.
HOST_NAMES = {
    "zenodo.org": "Zenodo",
    "github.com": "GitHub",
    "gitlab.com": "GitLab",
    "bitbucket.org": "Bitbucket",
    "codeberg.org": "Codeberg",
    "osf.io": "OSF",
    "dataverse.harvard.edu": "Harvard Dataverse",
    "figshare.com": "figshare",
    "datadryad.org": "Dryad",
    "huggingface.co": "Hugging Face",
    "codeocean.com": "Code Ocean",
    "kaggle.com": "Kaggle",
    "sourceforge.net": "SourceForge",
}
# DOI prefix -> host, used when a doi.org link isn't followed (--no-check).
DOI_PREFIX_HOSTS = {
    "10.5281": "Zenodo",
    "10.7910": "Harvard Dataverse",
    "10.17605": "OSF",
    "10.6084": "figshare",
    "10.5061": "Dryad",
    "10.24433": "Code Ocean",
    "10.57967": "Hugging Face",
}
# Namespaces ICSAC owns. Policy: ICSAC links to authors' code and doesn't host
# it, so a link into one of these is flagged for a look.
ICSAC_NAMESPACES = {
    "github.com": {"icsac", "icsac-institute"},
    "huggingface.co": {"icsac", "icsac-institute"},
}
DOI_LINK = re.compile(r"^https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/\S+)$", re.I)
INSTITUTE_DOI = re.compile(r"^10\.67697/", re.I)


class BuildError(Exception):
    """The index can't be built; nothing is written."""


# ── source ──────────────────────────────────────────────────────────────────

def git_show(repo: Path, ref: str, path: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"couldn't read {path} at {ref}: {r.stderr.strip() or 'git show failed'}")
    return r.stdout


def git_fetch(repo: Path) -> None:
    r = subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"couldn't fetch the website repository: {r.stderr.strip() or 'git fetch failed'}")


def parse_volumes(ts: str) -> list[dict]:
    """The PERSISTENCE_VOLUMES rows from persistence.ts, oldest first."""
    m = re.search(r"PERSISTENCE_VOLUMES[^=]*=\s*\[(.*?)\];", ts, re.S)
    rows = re.findall(
        r"\{\s*volume:\s*'([^']+)',\s*from:\s*'(\d{4}-\d{2}-\d{2})',\s*year:\s*'(\d{4})'\s*,?\s*\}",
        m.group(1) if m else "")
    if not rows:
        raise BuildError("couldn't find the volume table (PERSISTENCE_VOLUMES) in persistence.ts")
    return sorted(({"volume": v, "from": f, "year": y} for v, f, y in rows), key=lambda r: r["from"])


def volume_for(accepted: str, volumes: list[dict]) -> dict | None:
    d = (accepted or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return None
    hit = None
    for row in volumes:
        if d >= row["from"]:
            hit = row
    return hit


def load_registry(text: str) -> list[dict]:
    try:
        reg = json.loads(text)
    except json.JSONDecodeError as e:
        raise BuildError(f"the registry isn't valid JSON: {e}")
    if not isinstance(reg, list):
        raise BuildError("the registry isn't a list of articles")
    for i, e in enumerate(reg):
        missing = [k for k in REQUIRED if not str((e or {}).get(k) or "").strip()]
        if missing:
            raise BuildError(f"registry row {i} ({(e or {}).get('slug') or 'no slug'}) is missing {', '.join(missing)}")
    return reg


# ── links ───────────────────────────────────────────────────────────────────

def bare_doi(doi: str) -> str:
    return re.sub(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", (doi or "").strip(), flags=re.I)


def host_label(url: str) -> str:
    """'GitHub (owner)', 'Zenodo', or the bare hostname."""
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    m = DOI_LINK.match(url)
    if m:
        prefix = bare_doi(m.group(1)).split("/", 1)[0]
        return DOI_PREFIX_HOSTS.get(prefix, f"DOI {prefix}")
    name = next((v for k, v in HOST_NAMES.items() if host == k or host.endswith("." + k)), host or "unknown")
    if name == "GitHub":
        owner = [s for s in p.path.split("/") if s][:1]
        if owner:
            return f"GitHub ({owner[0]})"
    return name


def in_icsac_namespace(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    owners = ICSAC_NAMESPACES.get(host)
    if not owners:
        return False
    segs = [s.lower() for s in p.path.split("/") if s]
    if host == "huggingface.co" and segs[:1] and segs[0] in ("datasets", "spaces", "models"):
        segs = segs[1:]
    return bool(segs) and segs[0] in owners


def check_link(url: str, retries: int = 1) -> tuple[str, str | None]:
    """(status, final URL). Status is 'reachable' or "couldn't reach (reason)"."""
    if not re.match(r"^https?://", url, re.I):
        return "couldn't reach (not a web link)", None
    last = "couldn't reach"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                r.read(512)
                return "reachable", r.geturl()
        except urllib.error.HTTPError as e:
            last = f"couldn't reach (HTTP {e.code})"
            if e.code in (404, 410):
                return last, None
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                last = "couldn't reach (timed out)"
            elif isinstance(reason, socket.gaierror):
                last = "couldn't reach (host not found)"
            else:
                last = f"couldn't reach ({str(reason)[:60]})"
        except (socket.timeout, TimeoutError):
            last = "couldn't reach (timed out)"
        except Exception as e:  # a malformed response must not end the run
            last = f"couldn't reach ({type(e).__name__})"
        if attempt < retries:
            time.sleep(5)
    return last, None


def crossref_supplement(doi: str) -> tuple[str, list[str]]:
    """('ok', ids) or ("couldn't check (reason)", []) for an ICSAC-prefix DOI."""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            msg = json.load(r).get("message") or {}
    except Exception as e:
        return f"couldn't check ({type(e).__name__}: {str(e)[:60]})", []
    rel = (msg.get("relation") or {}).get("is-supplemented-by") or []
    return "ok", [str(x.get("id") or "").strip() for x in rel if x.get("id")]


# ── build ───────────────────────────────────────────────────────────────────

def build_rows(registry: list[dict], volumes: list[dict], check: bool) -> list[dict]:
    ordered = sorted(registry, key=lambda e: (e["accepted_date"][:10], bare_doi(e["doi"]).lower()))
    rows = []
    for e in ordered:
        vol = volume_for(e["accepted_date"], volumes)
        link = (e.get("code_data_url") or "").strip() or None
        row = {
            "volume": vol["volume"] if vol else None,
            "volume_year": vol["year"] if vol else None,
            "doi": bare_doi(e["doi"]),
            "title": re.sub(r"\s+", " ", e["title"]).strip(),
            "article_url": f"{SITE}/publications/{e['slug']}",
            "accepted": e["accepted_date"][:10],
            "code_data_url": link,
            "hosted_on": None,
            "hosted_by": None,
            "link_status": None,
        }
        if link:
            row["hosted_by"] = "ICSAC" if in_icsac_namespace(link) else "Author"
            if check:
                status, final = check_link(link)
                row["link_status"] = status
                row["hosted_on"] = host_label(final or link)
            else:
                row["link_status"] = "not checked"
                row["hosted_on"] = host_label(link)
        rows.append(row)
    return rows


def md(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def render_readme(rows: list[dict], updated: str) -> str:
    out = io.StringIO()
    w = out.write
    w("# Persistence: code and data index\n\n")
    w("Links to the code and data behind each article in *Persistence*, the journal of the "
      "Institute for Complexity Science and Advanced Computing (ICSAC).\n\n")
    w("This repository holds links only. Each author keeps their code and data wherever they "
      "choose, and ICSAC links to it. ICSAC doesn't host, run or maintain the linked material, "
      "and nothing here is a copy of it.\n\n")
    w("The table is generated from the journal's article registry, the same record each article "
      f"page on [icsacinstitute.org]({SITE}/journal/) is built from. Please don't edit it by hand: "
      "send corrections to help@icsacinstitute.org.\n\n")
    w("Each link is checked whenever the index is regenerated. A link that can't be reached is "
      "marked \"Couldn't reach\" and stays listed.\n")
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["volume"], r["volume_year"]), []).append(r)
    # Newest volume first, as /journal lists them; rows inside a volume in acceptance order.
    for (vol, year) in sorted(groups, key=lambda k: (k[0] is None, -int(k[1] or 0), k[0] or "")):
        w(f"\n## Volume {vol} ({year})\n\n" if vol else "\n## Not assigned to a volume\n\n")
        w("| Article | DOI | Code and data | Hosted by | Link check |\n")
        w("|---|---|---|---|---|\n")
        for r in groups[(vol, year)]:
            article = f"[{md(r['title'])}](<{r['article_url']}>)"
            doi = f"[{md(r['doi'])}](<https://doi.org/{r['doi']}>)"
            if r["code_data_url"]:
                code = f"[{md(r['code_data_url'])}](<{r['code_data_url']}>)"
                hosted = f"{r['hosted_by']}, on {md(r['hosted_on'])}"
                check = {"reachable": "Reachable", "not checked": "Not checked"}.get(
                    r["link_status"], md(r["link_status"][:1].upper() + r["link_status"][1:]))
            else:
                code, hosted, check = "No link on record", "", ""
            w(f"| {article} | {doi} | {code} | {hosted} | {check} |\n")
    w("\nMachine-readable copies: [index.json](index.json), [index.csv](index.csv).\n\n")
    w(f"Last changed: {updated}.\n")
    return out.getvalue()


def render_csv(rows: list[dict]) -> str:
    cols = ["volume", "volume_year", "doi", "title", "article_url", "accepted",
            "code_data_url", "hosted_on", "hosted_by", "link_status"]
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    wr.writeheader()
    for r in rows:
        wr.writerow({k: ("" if r.get(k) is None else r[k]) for k in cols})
    return buf.getvalue()


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--site-repo", type=Path, default=Path(__file__).resolve().parents[2] / "icsacinstitute.org",
                    help="website repository checkout (default: a sibling directory named icsacinstitute.org)")
    ap.add_argument("--ref", default="origin/main", help="git ref to read the registry at (default origin/main)")
    ap.add_argument("--fetch", action="store_true", help="git fetch the website repository first")
    ap.add_argument("--registry", type=Path, help="read the registry from this file instead of git")
    ap.add_argument("--volumes", type=Path, help="read persistence.ts from this file instead of git")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1],
                    help="directory to write README.md, index.json, index.csv (default: this repository)")
    ap.add_argument("--no-check", action="store_true", help="don't fetch the links (rows read 'Not checked')")
    ap.add_argument("--no-crossref", action="store_true", help="skip the Crossref cross-check")
    ap.add_argument("--dry-run", action="store_true", help="print the README; write nothing")
    ap.add_argument("--today", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    try:
        if a.fetch and not a.registry:
            git_fetch(a.site_repo)
        reg_text = a.registry.read_text(encoding="utf-8") if a.registry else git_show(a.site_repo, a.ref, REGISTRY_PATH)
        vol_text = a.volumes.read_text(encoding="utf-8") if a.volumes else git_show(a.site_repo, a.ref, VOLUMES_PATH)
        registry = load_registry(reg_text)
        volumes = parse_volumes(vol_text)
    except (BuildError, OSError) as e:
        print(f"[code-index] couldn't build: {e}", file=sys.stderr)
        return 2

    rows = build_rows(registry, volumes, check=not a.no_check)
    if len(rows) != len(registry):  # never cut silently
        print(f"[code-index] couldn't build: {len(registry)} registry rows but {len(rows)} index rows", file=sys.stderr)
        return 2

    needs_look = []
    for r in rows:
        if r["link_status"] and r["link_status"].startswith("couldn't"):
            needs_look.append(f"{r['doi']}: {r['link_status']} ({r['code_data_url']})")
        if r["hosted_by"] == "ICSAC":
            needs_look.append(f"{r['doi']}: the code link points into an ICSAC namespace ({r['code_data_url']})")
    if not a.no_crossref:
        for r in rows:
            if not INSTITUTE_DOI.match(r["doi"]):
                continue
            state, ids = crossref_supplement(r["doi"])
            mine = r["code_data_url"]
            want = (bare_doi(DOI_LINK.match(mine).group(1)) if mine and DOI_LINK.match(mine) else mine)
            if state != "ok":
                needs_look.append(f"{r['doi']}: Crossref {state}")
            elif (want or None) and want.lower() not in [i.lower() for i in ids]:
                needs_look.append(f"{r['doi']}: Crossref is-supplemented-by {ids or 'is empty'}, registry has {want}")
            elif not want and ids:
                needs_look.append(f"{r['doi']}: Crossref is-supplemented-by {ids}, registry has no link")
            else:
                print(f"[code-index] {r['doi']}: Crossref agrees ({ids or 'no link on either'})")

    today = a.today or dt.date.today().isoformat()
    updated = today
    index_path = a.out / "index.json"
    try:
        prev = json.loads(index_path.read_text(encoding="utf-8"))
        if prev.get("articles") == rows and prev.get("updated"):
            updated = prev["updated"]
    except (OSError, ValueError):
        pass

    doc = {
        "title": "Persistence: code and data index",
        "publisher": "Institute for Complexity Science and Advanced Computing (ICSAC)",
        "journal_url": f"{SITE}/journal/",
        "policy": "Links only. Authors host their own code and data; ICSAC links to it and doesn't host, run or maintain it.",
        "updated": updated,
        "articles": rows,
    }
    readme = render_readme(rows, updated)
    with_link = sum(1 for r in rows if r["code_data_url"])
    print(f"[code-index] {len(registry)} article(s) in the registry, {len(rows)} row(s); "
          f"{with_link} with a code/data link, {len(rows) - with_link} with none on record")
    for line in needs_look:
        print(f"[code-index] NEEDS A LOOK: {line}")

    if a.dry_run:
        sys.stdout.write(readme)
        return 1 if needs_look else 0
    a.out.mkdir(parents=True, exist_ok=True)
    write_atomic(a.out / "README.md", readme)
    write_atomic(index_path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    write_atomic(a.out / "index.csv", render_csv(rows))
    print(f"[code-index] wrote README.md, index.json, index.csv (last changed {updated})")
    return 1 if needs_look else 0


if __name__ == "__main__":
    sys.exit(main())
