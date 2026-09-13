"""Read-only structural checks for the investment wiki; no factual verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

TYPES = {"entities": "entity", "concepts": "concept", "sources": "source_summary",
         "queries": "query", "synthesis": "synthesis", "comparisons": "comparison"}
REQUIRED = {"type", "domain", "status", "verification", "created", "updated", "as_of", "summary"}
SOURCE_FIELDS = {"author", "source_type", "source_id", "source_url", "published_at",
                 "fetched_at", "compiled_at", "raw_source", "source_hash"}


def frontmatter(text: str) -> dict:
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", text, re.S)
    return (yaml.safe_load(match[1]) or {}) if match else {}


def links(text: str) -> list[str]:
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[ \t]*$", "", text)
    text = re.sub(r"`[^`\n]*`", "", text)
    return re.findall(r"\[\[([^\]\n]+)\]\]", text)


def target(vault: Path, current: Path, link: str) -> tuple[Path, str]:
    base, _, anchor = link.replace("\\|", "|").split("|", 1)[0].partition("#")
    path = (vault / base).resolve() if base else current
    if not path.is_relative_to(vault):
        raise ValueError("link escapes vault")
    if not path.is_file():
        path = Path(str(path) + ".md")
    return path, anchor


def lint(root: Path) -> dict:
    root = root.resolve()
    config = json.loads((root / ".llm-wiki/config.json").read_text(encoding="utf-8"))
    vault = Path(config["vault"]).resolve()
    if not root.is_relative_to(vault) or not (vault / ".obsidian").is_dir():
        raise ValueError("Wiki must be inside the configured Obsidian vault.")
    errors, warnings = [], []
    for name in ("purpose.md", "schema.md", "AGENTS.md", "wiki/index.md", "wiki/log.md", "wiki/overview.md"):
        if not (root / name).is_file():
            errors.append(f"Missing required file: {name}")
    for name in (*("wiki/" + d for d in TYPES), "raw/sources", "raw/assets", ".llm-wiki/imports", ".llm-wiki/chats"):
        if not (root / name).is_dir():
            errors.append(f"Missing directory: {name}")
    pages = sorted((root / "wiki").rglob("*.md"))
    texts, metadata, outgoing = {}, {}, {}
    link_count = 0
    for page in [*pages, *root.glob("*.md")]:
        body = page.read_text(encoding="utf-8-sig")
        texts[page] = body
        try:
            meta = frontmatter(body)
            if not isinstance(meta, dict):
                raise ValueError("frontmatter must be a mapping")
        except (ValueError, yaml.YAMLError) as exc:
            errors.append(f"Invalid YAML: {page.name}: {exc}")
            meta = {}
        metadata[page] = meta
        outgoing[page] = set()
        if page in pages and page.name != "log.md":
            missing = REQUIRED - meta.keys()
            if missing:
                errors.append(f"Missing metadata: {page.name}: {sorted(missing)}")
            expected = TYPES.get(page.parent.name, {"index.md": "index", "overview.md": "overview"}.get(page.name))
            if meta.get("type") != expected or expected is None:
                errors.append(f"Wrong page type/location: {page.name}")
            if meta.get("status") not in {"draft", "compiled", "superseded"}:
                errors.append(f"Invalid compilation status: {page.name}")
            if meta.get("verification") not in {"pending", "partial", "verified", "disputed", "not_applicable"}:
                errors.append(f"Invalid verification status: {page.name}")
        for link in links(body):
            link_count += 1
            try:
                dest, anchor = target(vault, page, link)
                if not dest.is_file():
                    raise ValueError("target missing")
                outgoing[page].add(dest)
                if anchor:
                    dest_text = dest.read_text(encoding="utf-8-sig")
                    headings = {h.rstrip("# ") for h in re.findall(r"(?m)^#{1,6}\s+(.+)$", dest_text)}
                    if anchor.startswith("^"):
                        if not re.search(r"(?m)\^" + re.escape(anchor[1:]) + r"\s*$", dest_text):
                            raise ValueError("block missing")
                    elif anchor not in headings:
                        raise ValueError("heading missing")
            except (ValueError, UnicodeError) as exc:
                errors.append(f"Broken link in {page.name}: {link}: {exc}")
    catalog = outgoing.get(root / "wiki/index.md", set())
    for page in pages:
        if page.name not in {"index.md", "log.md"} and page not in catalog:
            errors.append(f"Not catalogued: {page.name}")
    archive = json.loads((root / ".llm-wiki/archive-manifest.json").read_text(encoding="utf-8"))["files"]
    for relative, entry in archive.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root / "raw") or not path.is_file():
            errors.append(f"Missing/unsafe archive path: {relative}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            errors.append(f"Archive hash mismatch: {relative}")
    for path in (root / "raw").rglob("*"):
        if path.is_file() and path.relative_to(root).as_posix() not in archive:
            errors.append(f"Unregistered raw file: {path.name}")
    source_count = 0
    covered = set()
    for page, meta in metadata.items():
        if meta.get("type") != "source_summary":
            continue
        source_count += 1
        missing = SOURCE_FIELDS - meta.keys()
        if missing:
            errors.append(f"Missing source fields: {page.name}: {sorted(missing)}")
        raw_links = links(str(meta.get("raw_source", "")))
        if len(raw_links) != 1:
            errors.append(f"Expected one raw source: {page.name}")
            continue
        raw, _ = target(vault, page, raw_links[0])
        if not raw.is_relative_to(root / "raw/sources") or not raw.is_file():
            errors.append(f"Invalid raw_source: {page.name}")
            continue
        covered.add(raw)
        raw_meta = frontmatter(raw.read_text(encoding="utf-8-sig"))
        expected_hash = raw_meta.get("content_hash") or hashlib.sha256(raw.read_bytes()).hexdigest()
        if meta.get("source_hash") != expected_hash:
            errors.append(f"Source version mismatch: {page.name}")
        if raw_meta.get("source_id") is not None and str(meta.get("source_id")) != str(raw_meta["source_id"]):
            errors.append(f"Source identity mismatch: {page.name}")
    for state_file in (root / ".llm-wiki/imports").glob("*.state.json"):
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for key, item in state["sources"].items():
            versions = item["versions"]
            current = [v for v in versions if v["source_hash"] == item["current"]]
            if len(current) != 1:
                errors.append(f"Invalid current source version: {key}")
            for version in versions:
                path = (vault / version["raw_path"]).resolve()
                if not path.is_relative_to(root / "raw/sources"):
                    errors.append(f"State path escapes sources: {key}")
                    continue
                entry = archive.get(path.relative_to(root).as_posix(), {})
                if entry.get("sha256") != version["file_hash"]:
                    errors.append(f"State/archive mismatch: {key}")
            if current and (vault / current[0]["raw_path"]).resolve() not in covered:
                warnings.append(f"Current source version not compiled: {key}")
    reviews = json.loads((root / ".llm-wiki/review-items.json").read_text(encoding="utf-8"))["items"]
    ids = set()
    for item in reviews:
        required = {"id", "kind", "page", "question", "status", "created_at", "resolution", "evidence"}
        if required - item.keys() or item.get("id") in ids:
            errors.append(f"Invalid/duplicate review item: {item.get('id')}")
        ids.add(item.get("id"))
        page = (root / item.get("page", "")).resolve()
        if not page.is_relative_to(root / "wiki") or not page.is_file():
            errors.append(f"Invalid review page: {item.get('id')}")
        if item.get("status") not in {"open", "resolved", "deferred"}:
            errors.append(f"Invalid review status: {item.get('id')}")
        if item.get("status") == "resolved" and not (item.get("evidence") and item.get("resolution")):
            errors.append(f"Resolved review lacks evidence: {item.get('id')}")
    return {"ok": not errors, "wiki_pages": len(pages), "source_cards": source_count,
            "links_checked": link_count, "archived_files": len(archive),
            "open_reviews": sum(i.get("status") == "open" for i in reviews),
            "errors": errors, "warnings": warnings,
            "scope": "Structure and archive integrity only; factual/semantic review remains separate."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        report = lint(args.root)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        report = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
