#!/usr/bin/env python3
"""Dump selected lectures (summary + transcript + OCR pages) to markdown.

Usage:
    python scripts/dump_lectures.py <db_path> <output_dir> [sub_id ...]

If no sub_ids are given, dumps the 10 most recently processed lectures.
Writes one markdown file per lecture.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys


def _fname_safe(s: str) -> str:
    return re.sub(r"[^\w\-一-鿿]", "_", s)[:60]


def dump_one(db: sqlite3.Connection, sub_id: str, out_dir: str) -> str:
    db.row_factory = sqlite3.Row
    lec = db.execute(
        "SELECT l.*, c.title AS course_title, c.teacher "
        "FROM lectures l LEFT JOIN courses c USING(course_id) "
        "WHERE sub_id = ?", (sub_id,),
    ).fetchone()
    if not lec:
        print(f"  [skip] no row for sub_id={sub_id}")
        return ""

    ppts = db.execute(
        "SELECT page_num, text AS ocr_text, ocr_status FROM ppt_pages "
        "WHERE sub_id = ? ORDER BY page_num", (sub_id,),
    ).fetchall()

    course_title = lec["course_title"] or "?"
    sub_title = lec["sub_title"] or sub_id
    fname = f"{sub_id}_{_fname_safe(course_title)}_{_fname_safe(sub_title)}.md"
    path = os.path.join(out_dir, fname)

    transcript = lec["transcript"] or ""
    summary = lec["summary"] or ""

    done_pages = [p for p in ppts if p["ocr_status"] == "done"]
    other_pages = [p for p in ppts if p["ocr_status"] != "done"]
    status_counts: dict[str, int] = {}
    for p in ppts:
        s = p["ocr_status"] or "?"
        status_counts[s] = status_counts.get(s, 0) + 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# [{course_title}] {sub_title}\n\n")
        f.write(f"- **sub_id**: {sub_id}  \n")
        f.write(f"- **course_id**: {lec['course_id']}  \n")
        f.write(f"- **date**: {lec['date']}  \n")
        f.write(f"- **teacher**: {lec['teacher']}  \n")
        f.write(f"- **model**: {lec['summary_model']}  \n")
        f.write(f"- **processed_at**: {lec['processed_at']}  \n")
        f.write(f"- **transcript chars**: {len(transcript)}  \n")
        f.write(f"- **summary chars**: {len(summary)}  \n")
        f.write(f"- **PPT pages**: " + ", ".join(
            f"{k}={v}" for k, v in sorted(status_counts.items())
        ) + "\n\n")

        f.write("---\n\n## Summary\n\n")
        f.write(summary or "_(empty)_")
        f.write("\n\n---\n\n## Transcript (full)\n\n```\n")
        f.write(transcript or "(empty)")
        f.write("\n```\n\n---\n\n## OCR'd PPT Pages (status=done)\n\n")
        for p in done_pages:
            txt = (p["ocr_text"] or "").strip()
            f.write(f"### Page {p['page_num']}\n\n```\n{txt}\n```\n\n")
        if other_pages:
            f.write("---\n\n## Non-done PPT Pages\n\n")
            for p in other_pages:
                f.write(f"- page {p['page_num']}: `{p['ocr_status']}`\n")

    print(f"  → {path}  ({len(summary)}+{len(transcript)} chars, "
          f"{len(done_pages)} OCR'd pages)")
    return path


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    db_path = sys.argv[1]
    out_dir = sys.argv[2]
    sub_ids = sys.argv[3:]
    os.makedirs(out_dir, exist_ok=True)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    if not sub_ids:
        sub_ids = [
            r["sub_id"] for r in db.execute(
                "SELECT sub_id FROM lectures WHERE summary IS NOT NULL "
                "ORDER BY processed_at DESC LIMIT 10"
            )
        ]

    print(f"Dumping {len(sub_ids)} lecture(s) to {out_dir}/")
    for sub_id in sub_ids:
        dump_one(db, str(sub_id), out_dir)


if __name__ == "__main__":
    main()
