"""
converters/run_all.py — Run all external schema converters
----------------------------------------------------------
Fetches and converts BIDS, NWB, DANDI, openMINDS, and AIND schemas
to LinkML YAML in the schemas/ directory.

Usage:
    python converters/run_all.py
    python converters/run_all.py --only bids nwb
    python converters/run_all.py --skip openminds
"""

from __future__ import annotations
import sys
from pathlib import Path
import click

# Add parent dir to path so converters can import each other
sys.path.insert(0, str(Path(__file__).parent.parent))

from converters import bids, nwb, dandi, openminds, aind

CONVERTERS = {
    "bids":       bids,
    "nwb":        nwb,
    "dandi":      dandi,
    "openminds":  openminds,
    "aind":       aind,
}


@click.command()
@click.option("--only", multiple=True,
              help="Run only these converters (e.g. --only bids --only nwb)")
@click.option("--skip", multiple=True,
              help="Skip these converters")
@click.option("--out-dir", default="schemas", show_default=True,
              help="Output directory for .yml files")
def cli(only: tuple, skip: tuple, out_dir: str) -> None:
    """Fetch and convert all external neuroscience schemas to LinkML."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    to_run = list(only) if only else list(CONVERTERS.keys())
    to_run = [k for k in to_run if k not in skip]

    print(f"Running converters: {', '.join(to_run)}")
    errors = []
    for name in to_run:
        converter = CONVERTERS.get(name)
        if not converter:
            print(f"  WARNING: unknown converter '{name}'")
            continue
        print(f"\n{'─'*50}")
        try:
            converter.run(out / f"{name}.yml")
        except Exception as e:
            print(f"  ERROR in {name}: {e}")
            errors.append(name)

    print(f"\n{'─'*50}")
    if errors:
        print(f"Completed with errors in: {', '.join(errors)}")
        sys.exit(1)
    else:
        print(f"All converters completed. Schemas written to {out}/")


if __name__ == "__main__":
    cli()
