# ═══════════════════════════════════════════════════════
# KHABAR — Weekly Rollup (trigger)
# ═══════════════════════════════════════════════════════
# The whole computation now lives inside the database as the function
# run_weekly_rollup(). This job just triggers it, so a run finishes in
# seconds instead of shipping every row out and back.
#
# The function is idempotent + self-healing: each run rebuilds the weekly
# summaries for every week still inside the raw snapshot window and writes a
# new week only when it differs from the week before (event-driven sparsity).
# It never deletes raw data.
# ═══════════════════════════════════════════════════════

import os
import sys
from supabase import create_client

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

if __name__ == "__main__":
    print("🚀 Khabar weekly rollup — triggering database function...")
    try:
        supabase.rpc("run_weekly_rollup").execute()
        s = supabase.table("weekly_product_summary").select("id", count="exact").limit(1).execute()
        v = supabase.table("weekly_variant_exception").select("id", count="exact").limit(1).execute()
        print(f"✅ Rollup complete. "
              f"Product-week rows: {s.count}. Variant-exception rows: {v.count}.")
    except Exception as e:
        print(f"❌ Rollup failed: {e}")
        sys.exit(1)
