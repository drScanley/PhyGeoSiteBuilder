#!/usr/bin/env python3
"""
build_site.py — assemble the interactive ancestral-range figure from source files.

Expected folder layout (all paths configurable via the constants below,
or via command-line flags — see `python build_site.py --help`):

    project/
      build_site.py       (this file)
      template.html         (the page shell — placeholders get filled in)
      site_config.json         (the header text: title, description, etc — edit freely)
      tree.svg                    (the cladogram, e.g. exported from R's ape::plot.phylo)
      manifest.csv                  (node, age, kind, label, filename)
      images/                          (all PNGs referenced in manifest.csv)

Run it with:

    python3 build_site.py

...and it writes `output/site.html`, a single self-contained file (all
images and the tree are embedded as base64 / inline SVG) ready to host
or open directly in a browser.

WHY A MANIFEST + SVG PARSE INSTEAD OF HARDCODED COORDINATES:
Node positions are read directly out of the tree SVG (matching each
numbered circle to its x/y), so this script keeps working even if the
underlying tree changes shape — you only maintain manifest.csv and the
images folder, never a coordinates table.

SITE_CONFIG.JSON:
Controls the page header — the eyebrow label, main title, browser tab
title, and the descriptive paragraph. Edit the values (they're plain
text, no HTML needed) and re-run the script. If this file is missing,
sensible generic defaults are used instead of failing.

MANIFEST.CSV COLUMNS:
    node      - integer, must match the node number printed in the tree SVG
    age       - float, age in Mya (0 for present-day tips)
    kind      - one of: root | internal | tip | fossil | family
    label     - species binomial or family name; blank for generic internal nodes
    filename  - PNG filename, must exist in the images/ folder

    Multiple rows sharing the same `node` become a time-series slider for
    that node, ordered by `age` ascending (this is how nodes 1, 4, and 5
    got their "drag along the branch" behaviour in the current figure).
"""

import argparse
import base64
import csv
import json
import re
import sys
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# 1. Parse the tree SVG: strip the XML declaration, pull out width/height,
#    and find every (node_number -> (x, y)) pair by matching each numbered
#    circle to the <text font-size="6"> label that immediately follows it.
# ---------------------------------------------------------------------------

NODE_CIRCLE_RE = re.compile(
    r'<circle cx="([\d.]+)" cy="([\d.]+)"[^>]*></circle>'
    r'<text[^>]*font-size="6">(\d+)</text>'
)
SVG_DIMS_RE = re.compile(r'<svg height="(\d+)" width="(\d+)"')


def parse_tree_svg(svg_path: Path):
    raw = svg_path.read_text(encoding="utf-8")
    raw = re.sub(r'^<\?xml[^>]*\?>\s*', '', raw).strip()

    dims = SVG_DIMS_RE.search(raw)
    if not dims:
        sys.exit(f"Could not find <svg height=... width=...> in {svg_path}")
    height, width = int(dims.group(1)), int(dims.group(2))

    coords = {}
    for cx, cy, node in NODE_CIRCLE_RE.findall(raw):
        coords[int(node)] = (float(cx), float(cy))

    if not coords:
        sys.exit(f"No numbered node circles found in {svg_path} — check the SVG format.")

    # Retarget the opening <svg> tag to a responsive viewBox version we can
    # scale with CSS, and give it an id so the page can style it.
    old_open_tag = f'<svg height="{height}" width="{width}" xmlns="http://www.w3.org/2000/svg">'
    new_open_tag = (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg" id="treeSvg">'
    )
    if old_open_tag not in raw:
        sys.exit("SVG opening tag didn't match the expected format — check for upstream changes.")
    raw = raw.replace(old_open_tag, new_open_tag)

    return raw, width, height, coords


DEFAULT_CONFIG = {
    "page_title": "Ancestral Range Explorer",
    "eyebrow": "Ancestral Range Reconstruction",
    "title": "Untitled Phylogeny",
    "description": "Hover or tap a marked node to see its reconstructed geographic range.",
}


def load_config(config_path: Path) -> dict:
    """Read the editable header text (title, description, etc). Missing file
    or missing keys fall back to DEFAULT_CONFIG so the script never breaks
    just because someone hasn't customized it yet."""
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            config.update(json.load(f))
    else:
        print(f"note: no {config_path} found — using default header text "
              f"(copy site_config.json to customize)", file=sys.stderr)
    return config


# ---------------------------------------------------------------------------
# 2. Read the manifest: group rows by node, sort each node's rows by age.
# ---------------------------------------------------------------------------

def load_manifest(csv_path: Path):
    by_node = defaultdict(list)
    with open(csv_path, newline='', encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_node[int(row["node"])].append({
                "age": float(row["age"]),
                "kind": row["kind"].strip(),
                "label": row["label"].strip(),
                "filename": row["filename"].strip(),
            })
    for node in by_node:
        by_node[node].sort(key=lambda r: r["age"])
    return by_node


# ---------------------------------------------------------------------------
# 3. Turn a node's manifest rows into display metadata (title, caption, etc).
# ---------------------------------------------------------------------------

def format_age(age: float, kind: str) -> str:
    if age == 0 and kind == "tip":
        return "present day"
    return f"{age:.1f} Mya"


def describe_node(node: int, rows: list) -> dict:
    first = rows[0]
    kind, label = first["kind"], first["label"]

    if kind == "root":
        sub, italic = "Root — internal node", False
    elif kind == "internal":
        sub, italic = "Internal node — ancestral range", False
    elif kind == "family":
        sub, italic = label, False
    elif kind == "fossil":
        sub, italic = f"{label} (fossil)", True
    elif kind == "tip":
        sub, italic = label, True
    else:
        sys.exit(f"Unknown kind {kind!r} for node {node}")

    if kind == "family":
        caption = ("Shaded region is the posterior probability surface for the "
                   "ancestral range of this family's most recent common ancestor.")
    elif kind == "tip":
        caption = "Species range shown reflects present-day occurrence."
    elif kind == "fossil":
        caption = "Range reflects the known fossil occurrence, used as a calibration point in the analysis."
    else:
        caption = "Shaded region is the posterior probability surface for this ancestral node's geographic range."

    return {
        "label": f"Node {node}",
        "sub": sub,
        "italic": italic,
        "age": format_age(first["age"], kind),
        "caption": caption,
    }


# ---------------------------------------------------------------------------
# 4. Build the JS data blob + hotspot HTML for every mapped node.
# ---------------------------------------------------------------------------

def encode_image(images_dir: Path, filename: str) -> str:
    path = images_dir / filename
    if not path.exists():
        sys.exit(f"Missing image referenced in manifest: {path}")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def build_nodes_js_and_hotspots(by_node, coords, images_dir, vb_w, vb_h):
    data_entries = []
    hotspot_tags = []

    for node in sorted(by_node):
        if node not in coords:
            print(f"warning: node {node} in manifest but not found in tree SVG — skipped", file=sys.stderr)
            continue

        rows = by_node[node]
        meta = describe_node(node, rows)
        x, y = coords[node]

        if len(rows) > 1:
            frame_items = []
            for r in rows:
                img = encode_image(images_dir, r["filename"])
                frame_items.append(f'{{ age: {r["age"]}, img: {json.dumps(img)} }}')
            frames_js = "[" + ", ".join(frame_items) + "]"
            default_img = encode_image(images_dir, rows[0]["filename"])
        else:
            frames_js = "null"
            default_img = encode_image(images_dir, rows[0]["filename"])

        data_entries.append(
            "  %d: { label: %s, sub: %s, age: %s, caption: %s, italic: %s, img: %s, frames: %s }" % (
                node,
                json.dumps(meta["label"]), json.dumps(meta["sub"]),
                json.dumps(meta["age"]), json.dumps(meta["caption"]),
                "true" if meta["italic"] else "false",
                json.dumps(default_img), frames_js,
            )
        )

        left_pct = x / vb_w * 100
        top_pct = y / vb_h * 100
        hotspot_tags.append(
            f'<button class="hotspot" style="left:{left_pct:.2f}%; top:{top_pct:.2f}%;" '
            f'data-node="{node}" aria-label="{meta["label"]}: {meta["sub"]}" tabindex="0"></button>'
        )

    nodes_js = "const nodeData = {\n" + ",\n".join(data_entries) + "\n};"
    hotspots_html = "\n        ".join(hotspot_tags)
    return nodes_js, hotspots_html, len(data_entries)


# ---------------------------------------------------------------------------
# 5. Glue it all together.
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tree", default="tree.svg", help="path to the tree SVG (default: tree.svg)")
    ap.add_argument("--manifest", default="manifest.csv", help="path to manifest.csv")
    ap.add_argument("--images", default="images", help="folder containing the PNG files")
    ap.add_argument("--template", default="template.html", help="path to the HTML template")
    ap.add_argument("--config", default="site_config.json", help="path to the header-text config JSON")
    ap.add_argument("--out", default="output/site.html", help="output HTML path")
    args = ap.parse_args()

    tree_path = Path(args.tree)
    manifest_path = Path(args.manifest)
    images_dir = Path(args.images)
    template_path = Path(args.template)
    config_path = Path(args.config)
    out_path = Path(args.out)

    config = load_config(config_path)

    tree_svg, vb_w, vb_h, coords = parse_tree_svg(tree_path)
    print(f"parsed tree: {len(coords)} node positions found ({vb_w}x{vb_h})")

    by_node = load_manifest(manifest_path)
    total_rows = sum(len(v) for v in by_node.values())
    print(f"manifest: {len(by_node)} nodes, {total_rows} image rows "
          f"({sum(1 for v in by_node.values() if len(v) > 1)} with a time-series)")

    nodes_js, hotspots_html, mapped_count = build_nodes_js_and_hotspots(
        by_node, coords, images_dir, vb_w, vb_h
    )

    html = template_path.read_text(encoding="utf-8")
    html = html.replace("__TREE_SVG__", tree_svg)
    html = html.replace("__HOTSPOTS__", hotspots_html)
    html = html.replace("__NODE_DATA__", nodes_js)
    html = html.replace("__PAGE_TITLE__", config["page_title"])
    html = html.replace("__EYEBROW__", config["eyebrow"])
    html = html.replace("__TITLE__", config["title"])
    html = html.replace("__DESCRIPTION__", config["description"])

    total_tree_nodes = max(coords.keys()) + 1  # assumes 0-indexed, contiguous
    time_series_nodes = sorted(n for n, rows in by_node.items() if len(rows) > 1)
    if mapped_count == total_tree_nodes:
        caption_summary = f"all {mapped_count} nodes mapped"
    else:
        caption_summary = f"{mapped_count} of {total_tree_nodes} nodes mapped"
    if time_series_nodes:
        names = ", ".join(str(n) for n in time_series_nodes)
        plural = "s" if len(time_series_nodes) > 1 else ""
        caption_summary += f" — node{plural} {names} include a branch time-series"
    html = html.replace("__CAPTION_SUMMARY__", caption_summary)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"wrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB, {mapped_count} nodes mapped)")


if __name__ == "__main__":
    main()
