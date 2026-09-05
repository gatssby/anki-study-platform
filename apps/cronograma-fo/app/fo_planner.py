"""Deterministic, side-effect-free source of truth for FO scheduling."""

from __future__ import annotations

import math
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping


DEFAULT_CUT_SUBJECTS = ("LIN", "ING", "INGLES", "INGLÊS", "ENGLISH")
REVIEW_FREE_KEYWORDS = ("revisao", "revisão", "livre")
SUBJECT_AREAS = {
    "BIO": "biologicas", "FIS": "exatas", "MAT": "exatas", "QUI": "exatas",
    "GEO": "humanas", "HIS": "humanas", "FIL": "humanas", "SOC": "humanas",
    "POR": "linguagens", "LIT": "linguagens", "LIN": "linguagens",
}
AREA_ORDER = ("exatas", "humanas", "biologicas", "linguagens")


def normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).upper().split())


@dataclass(frozen=True)
class Lesson:
    lesson_code: str
    slot_key: str
    subject_prefix: str
    subject_name: str
    module_number: int
    lesson_number: int
    recommended_date: date
    slot_index: int
    duration_seconds: int
    title: str

    @property
    def minutes(self) -> int:
        return max(1, int(round(self.effective_duration_seconds / 60)))

    @property
    def effective_duration_seconds(self) -> int:
        """Use the real duration; retain the established 45-minute fallback when absent."""
        return self.duration_seconds if self.duration_seconds > 0 else 45 * 60

    @property
    def area(self) -> str:
        return SUBJECT_AREAS.get(normalize(self.subject_prefix), "outras")


@dataclass(frozen=True)
class StudyDay:
    date_value: date
    capacity_percent: int
    capacity_minutes: int
    capacity_seconds: int


@dataclass(frozen=True)
class Assignment:
    day: StudyDay
    lesson: Lesson
    slot_index: int


@dataclass(frozen=True)
class UnallocatedLesson:
    lesson: Lesson
    duration_seconds: int
    max_capacity_seconds: int
    reason: str


@dataclass(frozen=True)
class FOPlan:
    start_date: date
    end_date: date
    include_weekends: bool
    lessons: tuple[Lesson, ...]
    days: tuple[StudyDay, ...]
    skipped_dates: tuple[date, ...]
    assignments: tuple[Assignment, ...]
    unallocated_lessons: tuple[UnallocatedLesson, ...]

    @property
    def is_feasible(self) -> bool:
        return not self.unallocated_lessons and len(self.assignments) == len(self.lessons)

    @property
    def capacity_mode(self) -> str:
        return "unlimited"

    @property
    def total_load_seconds(self) -> int:
        return sum(item.effective_duration_seconds for item in self.lessons)

    @property
    def estimated_duration_lesson_codes(self) -> tuple[str, ...]:
        return tuple(item.lesson_code for item in self.lessons if item.duration_seconds <= 0)

    @property
    def total_capacity_seconds(self) -> int:
        return sum(day.capacity_seconds for day in self.days)

    @property
    def raw_capacity_deficit_seconds(self) -> int:
        return 0

    @property
    def unallocated_load_seconds(self) -> int:
        return sum(item.duration_seconds for item in self.unallocated_lessons)

    @property
    def deficit_seconds(self) -> int:
        """Configured minutes are informational and never create a deficit."""
        return 0

    @property
    def first_date(self) -> date | None:
        return min((item.day.date_value for item in self.assignments), default=None)

    @property
    def last_date(self) -> date | None:
        return max((item.day.date_value for item in self.assignments), default=None)

    @property
    def empty_day_count(self) -> int:
        used = {item.day.date_value for item in self.assignments}
        return sum(day.date_value not in used for day in self.days)

    @property
    def max_daily_load_seconds(self) -> int:
        return max((row["used_seconds"] for row in self.daily_summary), default=0)

    @property
    def date_map(self) -> dict[str, str]:
        return {item.lesson.lesson_code: item.day.date_value.isoformat() for item in self.assignments}

    @property
    def daily_summary(self) -> list[dict[str, Any]]:
        by_date: dict[date, list[Assignment]] = defaultdict(list)
        for item in self.assignments:
            by_date[item.day.date_value].append(item)
        result = []
        for day in self.days:
            used_seconds = sum(item.lesson.effective_duration_seconds for item in by_date[day.date_value])
            result.append({
                "date": day.date_value.isoformat(),
                "lesson_count": len(by_date[day.date_value]),
                "used_seconds": used_seconds,
                "used_minutes": round(used_seconds / 60, 2),
                "minutes": round(used_seconds / 60, 2),
                "capacity_minutes": day.capacity_minutes,
                "capacity_seconds": day.capacity_seconds,
                "configured_capacity_ignored": True,
                "capacity_enforced": False,
                "remaining_seconds": None,
                "remaining_minutes": None,
                "lesson_codes": [item.lesson.lesson_code for item in by_date[day.date_value]],
            })
        return result


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {key: row[key] for key in row.keys()}


def select_eligible_fo_lessons(
    rows: Iterable[Any], cut_subjects: tuple[str, ...] = DEFAULT_CUT_SUBJECTS
) -> tuple[list[Lesson], dict[str, int], list[dict[str, Any]]]:
    """Convert database-like rows into the eligible, immutable FO lesson input."""
    cuts = {normalize(item) for item in cut_subjects}
    lessons: list[Lesson] = []
    cut_counts: Counter[str] = Counter()
    cut_examples: list[dict[str, Any]] = []
    for raw in rows:
        row = _row_dict(raw)
        if row.get("track_code") != "FO" or row.get("lesson_type") != "lesson" or int(row.get("is_seen") or 0):
            continue
        haystack = normalize(" ".join(str(row.get(key) or "") for key in (
            "title_raw", "portal_title", "subject_name", "subject_prefix", "module_label"
        )))
        values = {normalize(row.get("subject_prefix")), normalize(row.get("subject_name")), normalize(row.get("module_label"))}
        reason = None
        if int(row.get("is_cut") or 0):
            reason = row.get("cut_reason") or "already_cut"
        elif values & cuts:
            reason = "cut_subject"
        elif any(normalize(keyword) in haystack for keyword in REVIEW_FREE_KEYWORDS):
            reason = "review_free"
        if reason:
            cut_counts[str(reason)] += 1
            if len(cut_examples) < 10:
                cut_examples.append({"lesson_code": row["lesson_code"], "reason": reason})
            continue
        lessons.append(Lesson(
            lesson_code=row["lesson_code"], slot_key=row["slot_key"],
            subject_prefix=(row.get("subject_prefix") or "FO").strip() or "FO",
            subject_name=(row.get("subject_name") or "").strip(),
            module_number=int(row.get("module_number") or 9999),
            lesson_number=int(row.get("lesson_number") or 9999),
            recommended_date=date.fromisoformat(row["recommended_date"]),
            slot_index=int(row.get("slot_index") or 0),
            duration_seconds=int(row.get("duration_seconds") or 0),
            title=(row.get("portal_title") or row.get("title_raw") or "").strip(),
        ))
    return lessons, dict(cut_counts), cut_examples


def build_fo_plan(
    lessons: Iterable[Lesson], *, start_date: date, end_date: date,
    include_weekends: bool, capacity_percent_by_date: Mapping[date, int] | None = None,
    max_daily_minutes_weekday: int = 240, max_daily_minutes_saturday: int = 180,
    max_daily_minutes_sunday: int = 180,
) -> FOPlan:
    """Return an FO date plan without reading from or writing to a database."""
    if end_date < start_date:
        raise ValueError("A data final do planejamento FO é anterior à data inicial.")
    capacities = capacity_percent_by_date or {}
    days: list[StudyDay] = []
    skipped: list[date] = []
    current = start_date
    while current <= end_date:
        weekend = current.isoweekday() in {6, 7}
        percent = max(0, min(int(capacities.get(current, 100)), 100))
        if (weekend and not include_weekends) or percent <= 0:
            skipped.append(current); current += timedelta(days=1); continue
        base = max_daily_minutes_saturday if current.isoweekday() == 6 else max_daily_minutes_sunday if current.isoweekday() == 7 else max_daily_minutes_weekday
        # Capacity percentages are applied to seconds, rounded down deterministically.
        capacity_seconds = max(0, math.floor(base * 60 * percent / 100))
        days.append(StudyDay(current, percent, capacity_seconds // 60, capacity_seconds))
        current += timedelta(days=1)

    lesson_list = list(lessons)
    ordered = _pedagogical_order(lesson_list, start_date)
    assignments: list[Assignment] = []
    unallocated: list[UnallocatedLesson] = []
    if ordered and not days:
        unallocated.extend(
            UnallocatedLesson(lesson, lesson.effective_duration_seconds, 0, "no_eligible_days")
            for lesson in ordered
        )
    else:
        quotas = _weighted_lesson_quotas(len(ordered), days)
        ordered_lessons = iter(ordered)
        for day, quota in zip(days, quotas):
            for slot_index in range(1, quota + 1):
                assignments.append(Assignment(day, next(ordered_lessons), slot_index))
    return FOPlan(
        start_date, end_date, include_weekends, tuple(lesson_list), tuple(days), tuple(skipped),
        tuple(assignments), tuple(unallocated),
    )


def _group_by_subject(lessons: list[Lesson]) -> dict[str, list[Lesson]]:
    grouped: dict[str, list[Lesson]] = defaultdict(list)
    for lesson in lessons: grouped[lesson.subject_prefix].append(lesson)
    return grouped


def _pedagogical_order(lessons: list[Lesson], start: date) -> list[Lesson]:
    queues: dict[str, deque[Lesson]] = {
        subject: deque(sorted(items, key=lambda item: (
            item.module_number, item.lesson_number, item.recommended_date, item.slot_index, item.lesson_code
        )))
        for subject, items in _group_by_subject(lessons).items()
    }
    ordered: list[Lesson] = []
    last_subject = last_area = None
    subject_counts: Counter[str] = Counter()
    area_counts: Counter[str] = Counter()
    while any(queues.values()):
        area = _pick_area(queues, last_area, area_counts, start)
        subject = _pick_subject(queues, area, last_subject, subject_counts, start)
        lesson = queues[subject].popleft()
        ordered.append(lesson)
        last_subject, last_area = subject, lesson.area
        subject_counts[subject] += 1
        area_counts[lesson.area] += 1
    return ordered


def _weighted_lesson_quotas(total_lessons: int, days: list[StudyDay]) -> list[int]:
    """Balance counts by availability weight without imposing a daily limit."""
    if total_lessons <= 0 or not days:
        return [0] * len(days)
    if total_lessons < len(days):
        return [1 if index < total_lessons else 0 for index in range(len(days))]

    quotas = [1] * len(days)
    remainder = total_lessons - len(days)
    weight_total = sum(day.capacity_percent for day in days)
    exact = [remainder * day.capacity_percent / weight_total for day in days]
    floors = [math.floor(value) for value in exact]
    quotas = [base + extra for base, extra in zip(quotas, floors)]
    leftovers = remainder - sum(floors)
    order = sorted(
        range(len(days)),
        key=lambda index: (-(exact[index] - floors[index]), days[index].date_value),
    )
    for index in order[:leftovers]:
        quotas[index] += 1
    return quotas


def _area_key(area: str) -> tuple[int, str]:
    return (AREA_ORDER.index(area), area) if area in AREA_ORDER else (len(AREA_ORDER), area)


def _pick_area(queues: dict[str, deque[Lesson]], last: str | None, counts: Counter[str], start: date) -> str:
    areas = sorted({SUBJECT_AREAS.get(normalize(s), "outras") for s, q in queues.items() if q}, key=_area_key)
    choices = [a for a in areas if a != last] or areas
    def score(area: str) -> tuple[int, int, date, str]:
        items = [lesson for subject, queue in queues.items() if SUBJECT_AREAS.get(normalize(subject), "outras") == area for lesson in queue]
        return counts[area], -sum(x.recommended_date < start for x in items), min(x.recommended_date for x in items), area
    return min(choices, key=score)


def _pick_subject(queues: dict[str, deque[Lesson]], area: str, last: str | None, counts: Counter[str], start: date) -> str:
    available = [s for s, q in queues.items() if q]
    choices = [s for s in available if SUBJECT_AREAS.get(normalize(s), "outras") == area] or available
    choices = [s for s in choices if s != last] or choices
    def score(subject: str) -> tuple[int, int, date, int, str]:
        queue = queues[subject]
        return counts[subject], -sum(x.recommended_date < start for x in queue), min(x.recommended_date for x in queue), -len(queue), subject
    return min(choices, key=score)
