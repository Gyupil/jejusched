"""ParsedFile → CanonicalEvent 정규화. 설계서 §2.2.

연도 추정, 시간 파싱, title_norm, slot, 해시, judged 집합을 담당한다.
여기서 만든 `event_key`가 매칭 규칙 1의 기준이므로 규칙을 바꾸면 기존
캘린더의 키와 어긋난다는 점에 주의한다(§4-[6]).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, time, timedelta

from .models import CanonicalEvent, ParsedFile, ScheduleItem, Section

DEFAULT_DURATION_MIN = 60

WEEKDAYS = "월화수목금토일"  # date.weekday(): 월=0

# title_norm에서 지우는 괄호·따옴표류
_PUNCT = "「」『』<>〈〉“”‘’\"'"
_PUNCT_RE = re.compile(f"[{re.escape(_PUNCT)}]")
_MARK_PREFIX_RE = re.compile(r"^\s*\*+\s*")

_DATE_TEXT_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})")
_TIME_RE = re.compile(r"(\d{1,2})\s*:\s*(\d{2})")
_ALLDAY_WORDS = ("종일", "온종일", "하루종일")
# 종일 일정의 행사명 앞에 붙는 "(14:00)" — 시간은 note로 옮긴다
_LEADING_TIME_RE = re.compile(r"^\s*\(\s*(\d{1,2})\s*:\s*(\d{2})\s*\)\s*")


class NormalizeError(ValueError):
    """정규화 단계에서 행을 버려야 할 때."""


def _sha1(*parts: object) -> str:
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def hash_text(text: str) -> str:
    """`content_hash`용 정규화.

    원문의 줄바꿈 위치는 표의 칸 너비에 따라 달라질 뿐 내용이 아니다. 공백만
    다른 값을 "내용 변경"으로 보면 매번 불필요한 덮어쓰기가 생기므로 공백을
    전부 지우고 비교한다. 문장부호·대소문자는 실제 변경이므로 남긴다.
    """
    return re.sub(r"\s+", "", text or "")


def title_norm(title: str) -> str:
    """비교용 정규화. 공백·괄호·따옴표·대소문자·표시 접두를 모두 무시한다."""
    text = unicodedata.normalize("NFKC", title)
    text = _MARK_PREFIX_RE.sub("", text)
    text = re.sub(r"\s+", "", text)
    text = _PUNCT_RE.sub("", text)
    return text.lower()


def _join_lines(text: str, sep: str) -> str:
    return sep.join(ln.strip() for ln in text.split("\n") if ln.strip())


def split_title_and_note(raw_title: str) -> tuple[str, str]:
    """행사명에서 부가 문구(`*`/`※`로 시작하는 줄)를 떼어낸다.

    PDF/hwpx의 셀은 줄바꿈으로 접혀 있으므로 본문 줄은 공백으로 잇는다.
    """
    body: list[str] = []
    notes: list[str] = []
    for line in (ln.strip() for ln in raw_title.split("\n")):
        if not line:
            continue
        (notes if line[0] in "*※" else body).append(line)
    return " ".join(body), "\n".join(notes)


def parse_file_date(parsed: ParsedFile, reference_year: int) -> date:
    """파일 헤더('9월 7일 월요일') → 날짜.

    연도는 `reference_year`(보통 파일 mtime의 연도)에서 출발해 헤더의 요일과
    맞는 해로 ±1년 보정한다.
    """
    month, day = parsed.file_date_month, parsed.file_date_day
    want = parsed.file_weekday.strip()[:1]
    candidates: list[date] = []
    for year in (reference_year, reference_year - 1, reference_year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:  # 2/29 같은 경우
            continue
    if not candidates:
        raise NormalizeError(f"파일 날짜가 올바르지 않다: {month}/{day}")
    if want in WEEKDAYS:
        for cand in candidates:
            if WEEKDAYS[cand.weekday()] == want:
                return cand
    # 요일이 없거나 어느 해와도 맞지 않으면 기준 연도를 그대로 쓴다
    return candidates[0]


def resolve_event_date(date_text: str | None, file_date: date) -> date:
    """행의 날짜 셀 → 실제 날짜. 없으면 파일 날짜.

    연도는 {Y-1, Y, Y+1} 중 파일 날짜와 가장 가까운 해를 고른다(설계서 §2.2).
    연말·연초에 걸친 일정을 올바로 잡기 위한 규칙이다.
    """
    if not date_text:
        return file_date
    m = _DATE_TEXT_RE.search(date_text)
    if not m:
        return file_date
    month, day = int(m.group(1)), int(m.group(2))
    best: date | None = None
    for year in (file_date.year - 1, file_date.year, file_date.year + 1):
        try:
            cand = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((cand - file_date).days) < abs((best - file_date).days):
            best = cand
    if best is None:
        raise NormalizeError(f"날짜를 해석할 수 없다: {date_text!r}")
    return best


def parse_time_cell(time_text: str) -> tuple[bool, time | None, time | None]:
    """시간 셀 → (all_day, start, end). 종료가 없으면 start + 60분."""
    text = unicodedata.normalize("NFKC", time_text or "").strip()
    if any(word in text for word in _ALLDAY_WORDS):
        return True, None, None
    found = _TIME_RE.findall(text)
    if not found:
        return True, None, None  # 시간을 못 읽으면 종일로 둔다(정보 손실보다 낫다)
    hours = [(int(h) % 24, int(m)) for h, m in found]
    start = time(*hours[0])
    if len(hours) >= 2:
        end = time(*hours[1])
        # "18:00~01:00" 같은 자정 넘김은 종료를 그대로 두면 역전되므로 기본 길이로 대체
        if end <= start:
            end = _plus_minutes(start, DEFAULT_DURATION_MIN)
    else:
        end = _plus_minutes(start, DEFAULT_DURATION_MIN)
    return False, start, end


def _plus_minutes(base: time, minutes: int) -> time:
    moment = datetime.combine(date(2000, 1, 1), base) + timedelta(minutes=minutes)
    return moment.time() if moment.date() == date(2000, 1, 1) else time(23, 59)


def to_canonical(item: ScheduleItem, file_date: date) -> CanonicalEvent:
    """한 행 → CanonicalEvent."""
    section = Section.from_text(item.section)
    when = resolve_event_date(item.date_text, file_date)
    all_day, start, end = parse_time_cell(item.time_text)

    title, note = split_title_and_note(item.title)
    #  hwpx 파서는 글자 크기로 부기 줄을 이미 갈라 놓는다. PDF 픽스처는 `item.note`가
    #  빈 문자열이라 아래 결합이 아무 일도 하지 않는다 — content_hash가 그대로다.
    if item.note:
        note = "\n".join(filter(None, [item.note.strip(), note]))
    if all_day:
        # 종일인데 행사명이 "(14:00)…"로 시작하면 시간을 note로 보존한다
        m = _LEADING_TIME_RE.match(title)
        if m:
            title = _LEADING_TIME_RE.sub("", title)
            note = "\n".join(filter(None, [f"({m.group(1)}:{m.group(2)})", note]))

    location = _join_lines(item.location, " ")
    attendees = _join_lines(item.attendees, ", ")
    department = _join_lines(item.department, ", ")
    norm = title_norm(title)
    slot = "ALLDAY" if all_day else start.strftime("%H:%M")  # type: ignore[union-attr]

    return CanonicalEvent(
        date=when,
        section=section,
        all_day=all_day,
        start=start,
        end=end,
        title=title,
        title_norm=norm,
        location=location,
        attendees=attendees,
        department=department,
        note=note,
        slot=slot,
        event_key=_sha1(when.isoformat(), section.value, slot, norm),
        content_hash=_sha1(
            when.isoformat(), section.value, all_day, start, end,
            hash_text(title), hash_text(location), hash_text(attendees),
            hash_text(department), hash_text(note),
        ),
        row_index=item.row_index,
        highlight=item.highlight,
    )


def normalize(
    parsed: ParsedFile, reference_year: int
) -> tuple[date, list[CanonicalEvent], set[tuple[date, Section]], list[str]]:
    """ParsedFile → (파일 날짜, 이벤트 목록, judged 집합, 경고).

    judged = 새 파일이 일정을 하나라도 적은 (날짜, 구분) 중 파일 날짜 이후.
    이 집합에 든 (날짜, 구분)에만 "빠졌으면 `*` 표시"를 적용한다(설계서 §4-[4]).
    """
    file_date = parse_file_date(parsed, reference_year)
    warnings = list(parsed.warnings)

    events: list[CanonicalEvent] = []
    seen: dict[str, int] = {}
    for item in parsed.items:
        try:
            event = to_canonical(item, file_date)
        except (NormalizeError, ValueError) as exc:
            warnings.append(f"{item.row_index}행을 건너뛴다: {exc}")
            continue
        if event.event_key in seen:
            warnings.append(
                f"파일 내 중복({item.row_index}행, 첫 행 {seen[event.event_key]} 유지): "
                f"{event.date} [{event.section.value}] {event.slot} {event.title}"
            )
            continue
        seen[event.event_key] = item.row_index
        events.append(event)

    judged = {(e.date, e.section) for e in events if e.date >= file_date}
    return file_date, events, judged, warnings
