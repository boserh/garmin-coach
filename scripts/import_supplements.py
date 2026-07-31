"""One-off import of a user's current supplement list into the "Аналізи" tab.

Seeds ``Supplement`` rows from a hardcoded list (this user's own pasted supplement
table) via the same ``app.db.supplements.create_supplement`` the web form uses — a
one-time backfill, not a living CLI command (same "one-off" shelf as
``scripts/oneoff_plan_fixes.py``).

Run with the venv interpreter::

    ./venv/bin/python -m scripts.import_supplements --email me@example.com --dry-run
    ./venv/bin/python -m scripts.import_supplements --email me@example.com

Re-running is NOT idempotent — it always inserts a fresh row per item, so a repeat run
would duplicate everything. Re-run ``--dry-run`` first if unsure whether it already ran;
delete the duplicates via ``/checkups/supplements`` if it does happen.
"""
import argparse
import sys

from app.cli import _run, cli_user
from app.db import supplements as supplements_db

# name, dosage, frequency (timing/how-to-take), notes (what-for) — parsed from the
# user's own table (morning / midday / evening groups folded into `frequency`).
SUPPLEMENTS = [
    dict(name="ProHealth Liposomal NMN Pro 300", dosage=None,
        frequency="вранці, після сніданку",
        notes="↑ NAD+, клітинна енергія, довголіття"),
    dict(name="California Gold Nutrition Creatine Monohydrate", dosage=None,
        frequency="зранку після сніданку, щодня",
        notes="сила, витривалість, м'язи"),
    dict(name="Life Extension Super Omega-3 (EPA/DHA)", dosage=None,
        frequency="вранці, з їжею",
        notes="серце, мозок, протизапальний ефект; підсилює засвоєння жиророзчинних речовин"),
    dict(name="California Gold Nutrition D3 + K2 (MK-7)", dosage=None,
        frequency="вранці, обов'язково з жиром (разом з Omega-3)",
        notes="імунітет, кістки, судини. На зиму — замінити на слабшу дозу."),
    dict(name="Lion's Mane (Hericium erinaceus)", dosage=None,
        frequency="вранці, з їжею, для фокусу",
        notes="мозок, концентрація, пам'ять"),
    dict(name="Life Extension NAC", dosage="600 mg",
        frequency="після їжі, з водою",
        notes="антиоксидант, печінка, імунітет"),
    dict(name="Life Extension Curcumin Elite", dosage=None,
        frequency="з жирною їжею або з Omega-3",
        notes="протизапальний, суглоби"),
    dict(name="California Gold Nutrition Glucosamine, Chondroitin, MSM + Hyaluronic Acid",
        dosage=None, frequency="після їжі",
        notes="відновлення хряща, суглоби"),
    dict(name="Life Extension Taurine", dosage="1000 mg",
        frequency="за 30-60 хв до сну",
        notes="нерви, серце, релакс"),
    dict(name="California Gold Nutrition CollagenUP (Collagen I/III + Vit C + HA 60 мг)",
        dosage=None, frequency="ввечері, з водою, окремо від білкової їжі",
        notes="шкіра, сухожилля, зв'язки"),
    dict(name="Doctor's Best Hyaluronic Acid + Chondroitin Sulfate (BioCell Collagen)",
        dosage="HA 100 мг", frequency="ввечері, з CollagenUP або відразу після",
        notes="колаген II, суглоби"),
    dict(name="Life Extension Magnesium Glycinate", dosage="105 мг",
        frequency="ввечері", notes=None),
]


async def _import(email: str, dry_run: bool) -> int:
    async with cli_user(email) as (session, user):
        print(f"Importing {len(SUPPLEMENTS)} supplements for {email} (user_id={user.id})"
              + (" [dry-run]" if dry_run else "") + ":")
        for s in SUPPLEMENTS:
            print(f"  - {s['name']}" + (f" ({s['dosage']})" if s['dosage'] else ""))
            if not dry_run:
                await supplements_db.create_supplement(
                    session, user.id,
                    name=s["name"], dosage=s["dosage"], frequency=s["frequency"],
                    notes=s["notes"],
                )
        if dry_run:
            print("\nDry run — nothing written. Re-run without --dry-run to import.")
        else:
            print(f"\nDone — {len(SUPPLEMENTS)} rows added. See /checkups/supplements.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--dry-run", action="store_true",
                       help="print what would be imported without writing to the DB")
    args = parser.parse_args()
    return _run(_import(args.email, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
