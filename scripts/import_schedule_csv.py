#!/usr/bin/env python3
"""
Standalone CLI script: import FIFA 2026 schedule from CSV into the database.

Usage:
    python scripts/import_schedule_csv.py data/fifa_2026_schedule_seed.csv [--dry-run]

Options:
    --dry-run   Validate and preview without writing to the database.
"""
import sys
import os
import argparse

# Allow running from project root or scripts/ dir
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def main():
    parser = argparse.ArgumentParser(description="Import FIFA 2026 schedule from CSV.")
    parser.add_argument("csv_file", help="Path to the CSV schedule file")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, do not write to DB")
    args = parser.parse_args()

    csv_path = os.path.abspath(args.csv_file)
    if not os.path.exists(csv_path):
        print(f"[ERROR] File not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Reading schedule from: {csv_path}")
    with open(csv_path, encoding="utf-8") as f:
        csv_content = f.read()

    # Create Flask app context
    from app import create_app
    app = create_app()

    with app.app_context():
        from schedule_import import import_schedule_from_csv

        if args.dry_run:
            print("[INFO] --- DRY RUN MODE (no database changes) ---")

        result = import_schedule_from_csv(csv_content, dry_run=args.dry_run)

        print(f"\n[RESULT] Status: {result.get('status', 'unknown')}")
        print(f"[RESULT] Message: {result.get('message', '')}")

        stats = result.get("stats", {})
        if stats:
            print(f"\n[STATS]")
            for k, v in stats.items():
                print(f"  {k}: {v}")

        errors = result.get("errors", [])
        if errors:
            print(f"\n[ERRORS] ({len(errors)} errors):")
            for err in errors:
                print(f"  - {err}")

        warnings = result.get("warnings", [])
        if warnings:
            print(f"\n[WARNINGS] ({len(warnings)} warnings):")
            for w in warnings:
                print(f"  - {w}")

        if result.get("status") == "error":
            print("\n[FAIL] Import failed due to validation errors.", file=sys.stderr)
            sys.exit(1)

        if args.dry_run:
            print("\n[INFO] Dry run complete. No changes were saved.")
        else:
            print("\n[OK] Schedule import complete.")


if __name__ == "__main__":
    main()
