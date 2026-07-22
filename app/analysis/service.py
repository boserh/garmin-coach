"""Thin facade over the analysis package (CODE-01).

The old flat ``analysis.service`` module grew six loosely-related responsibilities into
1000+ lines. It's now a package split by concern:

- :mod:`app.analysis.client` — Anthropic client pool, model/pricing config, ``CallStats``,
  ``AnalystError`` + status mapping, the dedicated Claude thread pool, ``_complete``.
- :mod:`app.analysis.cache` — dedup-cache keys and the shared context builders they hash.
- :mod:`app.analysis.reports` — analyze/ask/activity + weekly digest + compare + injury.
- :mod:`app.analysis.plans` — plan generation/edits/adaptation/weather + strength.

This module re-exports every public (and test-referenced private) symbol so that existing
imports — ``from app.analysis.service import run_analysis`` etc. — keep working unchanged.
``prompts`` stays a sibling module (imported by the submodules directly).

NB on monkeypatching: a test that patches a ``*_with_stats`` helper called *inside* a
``run_*`` wrapper must patch it on the submodule that defines it (e.g.
``app.analysis.plans.generate_plan_with_stats``), not here — patching this facade rebinds
only the facade's name, not the reference the wrapper resolves. Patching a symbol used
*directly* by a test (``service._get_client``, ``service._cache_key``) works via the
re-export, and shared objects (``service.settings``, ``service._clients``) are the same
instances the submodules use.
"""
from app.analysis.cache import (  # noqa: F401
    CACHE_TTL_S,
    _activity_cache_key,
    _as_dict,
    _ask_cache_key,
    _build_fitness_snapshot,
    _build_multisport,
    _cache_key,
    _compare_cache_key,
    _digest_cache_key,
    _insights_cache_key,
    _race_cache_key,
    _wrapped_cache_key,
)
from app.analysis.client import (  # noqa: F401
    FABLE_5,
    MODEL_ACTIVITY,
    MODEL_ASK,
    MODEL_COMPARE,
    MODEL_DAILY,
    MODEL_DEEP,
    MODEL_DIGEST,
    MODEL_HEALTH,
    MODEL_INJURY,
    MODEL_INSIGHTS,
    MODEL_PLAN,
    MODEL_PLAN_GEN,
    MODEL_PLAN_GEN_ALT,
    MODEL_RACE,
    MODEL_WRAPPED,
    OPUS_4_8,
    PLAN_GEN_MODELS,
    PRICES,
    SONNET_4_6,
    SONNET_5,
    AnalystError,
    CallStats,
    _clients,
    _complete,
    _complete_tools,
    _get_client,
    _run_claude,
    _status_error,
    resolve_plan_model,
)
from app.analysis.plans import (  # noqa: F401
    ADAPT_COMPLIANCE_WEEKS,
    ADAPT_CONS_DIST_MIN_FRAC,
    ADAPT_CONS_MOVE_MAX_DAYS,
    ADAPT_TAPER_DAYS,
    ADAPT_TAPER_DIST_MIN_FRAC,
    ADAPT_WINDOW_DAYS_DEFAULT,
    ADJUST_LEVELS,
    SICK_LOOKBACK_DAYS,
    SICK_WINDOW_DAYS,
    WEATHER_CONTEXT_DAYS,
    _coerce_edit,
    _coerce_plan,
    _days_to_target,
    _filter_ops_to_level,
    _filter_ops_to_window,
    _filter_sick_ops,
    _filter_weather_ops,
    _in_adapt_window,
    _plan_ops_with_stats,
    _recent_compliance,
    generate_plan_with_stats,
    generate_strength_with_stats,
    plan_adapt_with_stats,
    plan_adjust_level,
    plan_edit_with_stats,
    run_plan_adaptation,
    run_plan_edit,
    run_plan_extension,
    run_plan_generation,
    run_sick_check,
    run_strength_preview,
    run_weather_plan_check,
    sick_with_stats,
    weather_plan_with_stats,
)
from app.analysis.reports import (  # noqa: F401
    _DEFAULT_DAILY_Q,
    ASK_CONTEXT_MIN,
    ASK_DEFAULT_N,
    ASK_LIMIT_TEXT,
    ASK_TOOL_MAX_TOKENS,
    DIGEST_COMPLIANCE_WEEKS,
    DIGEST_RECORDS_DAYS,
    DIGEST_RECOVERY_DAYS,
    DIGEST_VOLUME_WEEKS,
    MAX_ASK_ROUNDS,
    MAX_ASK_TOTAL_TOKENS,
    RECORDS_CONTEXT_DAYS,
    _ask_tools,
    _run_ask_tool,
    _segments,
    _week_volume_summary,
    activity_payload,
    analyze,
    analyze_activity_with_stats,
    analyze_with_stats,
    build_health_alerts,
    build_injury_assessment,
    compare_with_stats,
    digest_with_stats,
    health_with_stats,
    injury_with_stats,
    insights_with_stats,
    race_plan_with_stats,
    run_activity_analysis,
    run_analysis,
    run_ask,
    run_ask_agent,
    run_compare,
    run_digest,
    run_health_alert,
    run_injury_check,
    run_insights,
    run_race_plan,
    run_wrapped,
    wrapped_with_stats,
)
from app.core.config import settings  # noqa: F401  re-exported: tests patch service.settings.*
