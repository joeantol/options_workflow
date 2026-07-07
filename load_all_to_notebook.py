#!/Users/joeandbabs/public-env/bin/python
"""
load_all_to_notebook.py

Uploads all PLAN_* and REVIEW_* PDFs from the options directory to NotebookLM
in reverse-week order: PLAN 26, REVIEW 25, PLAN 25, REVIEW 24, …, PLAN 02, REVIEW 02.

Skips files already present in the notebook (matched by title).

Usage:
    ./load_all_to_notebook.py           # upload all missing files
    ./load_all_to_notebook.py --dryrun  # show what would be uploaded
"""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

OUTPUT_DIR  = Path("/Users/joeandbabs/work/retirement/options")
NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID", "")


def build_upload_order() -> list[Path]:
    """Return files sorted: PLAN PDF, PLAN CSV, REVIEW PDF per week, descending."""
    by_week: dict[int, dict[str, Path]] = {}
    for f in OUTPUT_DIR.iterdir():
        m = re.match(r"(PLAN|REVIEW)_.*_Week_(\d+)\.(pdf|csv)$", f.name)
        if not m:
            continue
        kind, week, ext = m.group(1), int(m.group(2)), m.group(3)
        key = f"{kind}_{ext}"   # e.g. "PLAN_pdf", "PLAN_csv", "REVIEW_pdf"
        by_week.setdefault(week, {})[key] = f

    result: list[Path] = []
    for week in sorted(by_week, reverse=True):
        w = by_week[week]
        for key in ("PLAN_pdf", "PLAN_csv", "REVIEW_pdf"):
            if key in w:
                result.append(w[key])
    return result


async def _run(dryrun: bool) -> None:
    from notebooklm import NotebookLMClient

    files = build_upload_order()
    if not files:
        print("No PLAN/REVIEW PDFs found.")
        sys.exit(1)

    async with NotebookLMClient.from_storage() as client:
        existing_titles = set()
        for src in await client.sources.list(NOTEBOOK_ID):
            if src.title:
                existing_titles.add(src.title)

        to_upload = [f for f in files if f.name not in existing_titles]
        already   = [f for f in files if f.name in existing_titles]

        print(f"Files in upload order: {len(files)}")
        print(f"  Already in notebook: {len(already)}")
        print(f"  To upload:           {len(to_upload)}")

        if dryrun:
            print("\nDry run — would upload:")
            for f in to_upload:
                print(f"  {f.name}")
            if not to_upload:
                print("  (nothing — all already present)")
            return

        if not to_upload:
            print("Nothing to upload.")
            return

        print()
        for i, f in enumerate(to_upload, 1):
            print(f"[{i}/{len(to_upload)}] Uploading {f.name} …")
            await client.sources.add_file(NOTEBOOK_ID, str(f), wait=False)
            print(f"  Queued.")

    print("\nAll uploads complete.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bulk-load all PLAN/REVIEW PDFs and PLAN CSVs into NotebookLM"
    )
    ap.add_argument("--dryrun", action="store_true", help="Show what would be uploaded")
    args = ap.parse_args()

    if not NOTEBOOK_ID:
        print("NOTEBOOKLM_NOTEBOOK_ID not set in .env")
        sys.exit(1)

    asyncio.run(_run(args.dryrun))


if __name__ == "__main__":
    main()
