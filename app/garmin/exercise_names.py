"""Garmin exercise name codes -> readable Ukrainian names.

Keys are Garmin's `name` field from exerciseSets (the specific exercise, not the
coarse `category`). Unknown names are logged by garmin_client and fall back to a
prettified form — add them here as they show up.
"""

EXERCISE_NAMES = {
    "ALTERNATING_DUMBBELL_BICEPS_CURL": "поперемінне згинання на біцепс з гантелями",
    "BODY_WEIGHT_DIP": "віджимання на брусах",
    "DUMBBELL_BULGARIAN_SPLIT_SQUAT": "болгарські присідання з гантелями",
    "DUMBBELL_LUNGE": "випади з гантелями",
    "HANGING_LEG_RAISE": "підйоми ніг у висі",
    "HYPEREXTENSION": "гіперекстензія",
    "INCLINE_DUMBBELL_BENCH_PRESS": "жим гантелей на похилій лавці",
    "KETTLEBELL_ROW": "тяга гирі в нахилі",
    "SEATED_DUMBBELL_SHOULDER_PRESS": "жим гантелей сидячи",
    "STRAIGHT_ARM_PLANK_WITH_SHOULDER_TOUCH": "планка з доторканням до плеча",
    "TRICEPS_PRESS": "жим на трицепс",
    "TRX_INVERTED_ROW": "горизонтальні підтягування (TRX)",
    "V_UP": "складка",
    "WEIGHTED_LEG_CURL": "згинання ніг з вагою",
    "WIDE_GRIP_LAT_PULLDOWN": "тяга верхнього блоку широким хватом",
    "_90_DEGREE_STATIC_HOLD": "статичне утримання 90°",
}
