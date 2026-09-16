"""HwpxParser — 설계서 M8 / `docs/parser_design.md`.

두 층으로 지킨다.

* **합성 문서 테스트**: 손으로 만든 최소 hwpx로 격자 복원·부기 분리·시간 해석을
  검증한다. 원본이 없는 환경(새 클론, CI)에서도 돌기 때문에 파서가 조용히
  망가지는 것을 막는 진짜 그물이다.
* **원본 대조 테스트**: 실물 4개를 정답지(`docs/hwpx_fixtures`)와 필드 단위로
  맞춰 보고, 마지막에 §8 기준선을 **hwpx 원본으로** 다시 돌린다.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest

from jejusched.core.models import Section
from jejusched.core.normalize import normalize
from jejusched.parsers import for_path
from jejusched.parsers.base import ParseError
from jejusched.parsers.fixture_json import FixtureJsonParser
from jejusched.parsers.hwpx import HwpxParser
from jejusched.parsers.hwpx.charpr import CharStyle, load_char_styles

from conftest import (
    FIXTURE_NAMES,
    fixture,
    hwpx_fixture,
    hwpx_raw,
    requires_fixtures,
    requires_hwpx,
    SAME_TITLES,
    YEAR,
)

# ------------------------------------------------------------ 합성 hwpx 만들기

NS = (
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
    'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"'
)
#  본문 1300, 부기 1100, 빨간 글씨 — 실물에서 쓰는 값 그대로
CHAR_PRS = {
    "0": ("#000000", 1300),
    "1": ("#000000", 1100),
    "2": ("#FF0000", 1300),
}


def _runs(lines) -> str:
    """[(글자, charPrIDRef), ...] 또는 ["글자", ...] → 문단 XML."""
    out = []
    for line in lines:
        if isinstance(line, str):
            line = [(line, "0")]
        elif isinstance(line, tuple):
            line = [line]
        runs = "".join(
            f'<hp:run charPrIDRef="{ref}"><hp:t>{text}</hp:t></hp:run>' for text, ref in line
        )
        out.append(f"<hp:p>{runs}</hp:p>")
    return "".join(out)


def _tc(row, col, lines, *, row_span=1, col_span=1, header=False) -> str:
    return (
        f'<hp:tc header="{1 if header else 0}">'
        f"<hp:subList>{_runs(lines)}</hp:subList>"
        f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
        f'<hp:cellSpan colSpan="{col_span}" rowSpan="{row_span}"/>'
        f"</hp:tc>"
    )


def build_hwpx(tmp_path: Path, *, header_text: str, tables: list[str], name="t.hwpx") -> Path:
    """최소 hwpx 하나를 만든다. 실물과 같은 구조·네임스페이스를 쓴다."""
    char_prs = "".join(
        f'<hh:charPr id="{i}" height="{h}" textColor="{c}"/>' for i, (c, h) in CHAR_PRS.items()
    )
    header = f'<hh:head {NS}><hh:refList>{char_prs}</hh:refList></hh:head>'
    section = (
        f"<hs:sec {NS}>"
        f"<hp:p><hp:run charPrIDRef=\"0\"><hp:t>{header_text}</hp:t></hp:run></hp:p>"
        + "".join(f"<hp:p><hp:run charPrIDRef=\"0\">{t}</hp:run></hp:p>" for t in tables)
        + "</hs:sec>"
    )
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/header.xml", header)
        z.writestr("Contents/section0.xml", section)
    return path


def table(rows_xml: str, *, rows: int, cols: int = 6) -> str:
    return f'<hp:tbl rowCnt="{rows}" colCnt="{cols}">{rows_xml}</hp:tbl>'


HEADER_ROW = "<hp:tr>" + "".join(
    _tc(0, c, [label], header=True)
    for c, label in enumerate(["구 분", "시 간", "행   사   명", "장 소", "참 석", "주관부서"])
) + "</hp:tr>"


def simple_row(row, values, spans=None) -> str:
    """한 행. `values[i]`는 문자열이거나 줄 목록. `spans`는 {열: {"row_span": n}}."""
    spans = spans or {}
    cells = []
    for col, value in enumerate(values):
        lines = [value] if isinstance(value, (str, tuple)) else value
        cells.append(_tc(row, col, lines, **spans.get(col, {})))
    return "<hp:tr>" + "".join(cells) + "</hp:tr>"


def real_warnings(parsed) -> list[str]:
    """합성 문서는 표가 1개라 그 경고는 늘 뜬다 — 진짜 경고만 남긴다."""
    return [w for w in parsed.warnings if "표가" not in w]


# ------------------------------------------------------------------- 합성 테스트


def test_reads_header_date_and_a_single_row(tmp_path):
    path = build_hwpx(
        tmp_path,
        header_text="【9월 7일 월요일】",
        tables=[table(
            HEADER_ROW + simple_row(1, ["도지사", "10:00", "간부회의", "탐라홀", "실장", "총무과"]),
            rows=2,
        )],
    )
    parsed = HwpxParser().parse(path)
    assert (parsed.file_date_month, parsed.file_date_day, parsed.file_weekday) == (9, 7, "월")
    assert len(parsed.items) == 1
    item = parsed.items[0]
    assert (item.section, item.time_text, item.title) == ("도지사", "10:00", "간부회의")
    assert (item.location, item.attendees, item.department) == ("탐라홀", "실장", "총무과")
    assert item.row_index == 1 and item.table_index == 0
    assert real_warnings(parsed) == []


def test_merged_section_cell_is_inherited(tmp_path):
    """**병합이 이 파서의 급소다.** 세로 병합된 구분이 아래 행까지 이어져야 한다."""
    rows = (
        HEADER_ROW
        + simple_row(1, [["행정", "부지사"], "08:40", "주간정책회의", "탐라홀", "", "총무과"],
                     spans={0: {"row_span": 2}})
        #  둘째 행에는 구분 칸이 아예 없다 — hwpx는 병합된 칸을 내보내지 않는다
        + "<hp:tr>" + "".join([
            _tc(2, 1, ["10:00"]), _tc(2, 2, ["정례회"]), _tc(2, 3, ["도의회"]),
            _tc(2, 4, [""]), _tc(2, 5, ["정책기획관"]),
        ]) + "</hp:tr>"
    )
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=3)])
    )
    assert [i.section for i in parsed.items] == ["행정부지사", "행정부지사"]
    assert [i.title for i in parsed.items] == ["주간정책회의", "정례회"]


def test_runs_inside_one_paragraph_join_without_space(tmp_path):
    """서식이 바뀌면 한 문단이 여러 run으로 쪼개진다. 공백을 끼우면 안 된다(§2.2)."""
    title = [[("천마천 하천기본계획(변경) 수립(안)", "0"), ("에 따른 설명회", "0")]]
    rows = HEADER_ROW + simple_row(1, ["도지사", "10:00", title, "", "", ""])
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    )
    assert parsed.items[0].title == "천마천 하천기본계획(변경) 수립(안)에 따른 설명회"


def test_smaller_line_becomes_note_even_without_star(tmp_path):
    """`*`로 시작하지 않아도 **작은 글씨면 부기**다 — 이게 글자 크기로 가르는 이유다."""
    title = [("제46회 경기장 참관", "0"), ("(여자 개인전 DB, 육성종목)", "1")]
    rows = HEADER_ROW + simple_row(1, ["도지사", "13:00", title, "", "", ""])
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    )
    assert parsed.items[0].title == "제46회 경기장 참관"
    assert parsed.items[0].note == "(여자 개인전 DB, 육성종목)"


def test_same_size_second_line_stays_in_the_title(tmp_path):
    title = [("제454회 정례회", "0"), ("보건복지안전위원회 제1차 회의", "0")]
    rows = HEADER_ROW + simple_row(1, ["도지사", "10:00", title, "", "", ""])
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    )
    assert parsed.items[0].title == "제454회 정례회 보건복지안전위원회 제1차 회의"
    assert parsed.items[0].note == ""


def test_red_title_sets_highlight(tmp_path):
    rows = (
        HEADER_ROW
        + simple_row(1, ["도지사", "10:00", ("빨간 일정", "2"), "", "", ""])
        + simple_row(2, ["도지사", "11:00", ("검은 일정", "0"), "", "", ""])
    )
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=3)])
    )
    assert [i.highlight for i in parsed.items] == [True, False]


@pytest.mark.parametrize(
    "lines, expect_date, expect_time",
    [
        (["10:00"], None, "10:00"),
        (["종일"], None, "종일"),
        (["10:00", "~", "17:00"], None, "10:00~17:00"),
        (["10:00", "17:00"], None, "10:00~17:00"),  # `~`가 빠져도 범위로 읽는다
        (["9/5", "(토)", "14:00"], "9/5", "14:00"),
        (["9/5(토)", "14:00"], "9/5", "14:00"),
        (["09/05", "14:00"], "9/5", "14:00"),
    ],
)
def test_time_cell_splits_date_from_time(tmp_path, lines, expect_date, expect_time):
    rows = HEADER_ROW + simple_row(1, ["도지사", lines, "회의", "", "", ""])
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    )
    assert (parsed.items[0].date_text, parsed.items[0].time_text) == (expect_date, expect_time)


def test_column_joins_follow_the_design(tmp_path):
    """장소는 공백, 참석·주관부서는 `, `로 잇는다(§3.4)."""
    rows = HEADER_ROW + simple_row(1, [
        "도지사", "10:00", "회의",
        ["국립제주검역소", "업무지원시설", "회의실"],
        ["실장", "안전정책과장"],
        ["안전정책과", "보건정책과"],
    ])
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    )
    item = parsed.items[0]
    assert item.location == "국립제주검역소 업무지원시설 회의실"
    assert item.attendees == "실장, 안전정책과장"
    assert item.department == "안전정책과, 보건정책과"


def test_headerless_table_is_read_as_the_future_block(tmp_path):
    """머리글 없는 표는 1열이 날짜이고 전부 `실 일정`이다(§3.2)."""
    rows = (
        simple_row(0, [["9/8", "(화)"], "14:00", "감시체계 회의", "zoom", "과장", "건강위생과"])
        + simple_row(1, [["9/9", "(수)"], "10:00", "점검 회의", "검역소", "과장", "건강위생과"])
    )
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    )
    assert [i.section for i in parsed.items] == ["실 일정", "실 일정"]
    assert [i.date_text for i in parsed.items] == ["9/8", "9/9"]
    assert [i.row_index for i in parsed.items] == [0, 1]
    assert [i.table_index for i in parsed.items] == [0, 0]


def test_both_tables_are_read_in_document_order(tmp_path):
    today = table(
        HEADER_ROW + simple_row(1, ["도지사", "10:00", "오늘 회의", "", "", ""]), rows=2
    )
    future = table(
        simple_row(0, ["9/8", "14:00", "다음 회의", "", "", ""]), rows=1
    )
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[today, future])
    )
    assert [(i.table_index, i.title) for i in parsed.items] == [
        (0, "오늘 회의"), (1, "다음 회의")
    ]


def test_empty_rows_are_dropped(tmp_path):
    rows = (
        HEADER_ROW
        + simple_row(1, ["도지사", "10:00", "회의", "", "", ""])
        + simple_row(2, ["", "", "", "", "", ""])
    )
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=3)])
    )
    assert [i.title for i in parsed.items] == ["회의"]
    assert real_warnings(parsed) == []


def test_unknown_section_warns_and_falls_back(tmp_path):
    rows = HEADER_ROW + simple_row(1, ["미래부지사", "10:00", "회의", "", "", ""])
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    )
    assert parsed.items[0].section == "실 일정"
    assert any("모르는 구분" in w for w in parsed.warnings)


def test_line_break_inside_a_run_starts_a_new_line(tmp_path):
    """이 문서들에는 없지만 다른 작성자·판본에서는 나올 수 있다(§2.1)."""
    cell = (
        '<hp:tc header="0"><hp:subList><hp:p>'
        '<hp:run charPrIDRef="0"><hp:t>실장</hp:t><hp:lineBreak/><hp:t>과장</hp:t></hp:run>'
        '</hp:p></hp:subList>'
        '<hp:cellAddr colAddr="4" rowAddr="1"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>'
    )
    rows = HEADER_ROW + (
        "<hp:tr>"
        + _tc(1, 0, ["도지사"]) + _tc(1, 1, ["10:00"]) + _tc(1, 2, ["회의"]) + _tc(1, 3, [""])
        + cell + _tc(1, 5, ["총무과"])
        + "</hp:tr>"
    )
    parsed = HwpxParser().parse(
        build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    )
    assert parsed.items[0].attendees == "실장, 과장"


def test_filename_is_used_when_the_header_paragraph_is_missing(tmp_path):
    rows = HEADER_ROW + simple_row(1, ["도지사", "10:00", "회의", "", "", ""])
    path = build_hwpx(tmp_path, header_text="주  요  일  정",
                      tables=[table(rows, rows=2)], name="0907_주요일정.hwpx")
    parsed = HwpxParser().parse(path)
    assert (parsed.file_date_month, parsed.file_date_day, parsed.file_weekday) == (9, 7, "")
    assert any("파일명" in w for w in parsed.warnings)


# --------------------------------------------------------------- 실패 처리


def test_binary_hwp_is_rejected_clearly(tmp_path):
    path = tmp_path / "old.hwp"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(ParseError, match="구형 .hwp"):
        HwpxParser().parse(path)


def test_non_zip_raises_parse_error(tmp_path):
    path = tmp_path / "junk.hwpx"
    path.write_bytes(b"not a zip at all")
    with pytest.raises(ParseError):
        HwpxParser().parse(path)


def test_zip_without_a_body_raises_parse_error(tmp_path):
    path = tmp_path / "empty.hwpx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
    with pytest.raises(ParseError, match="본문"):
        HwpxParser().parse(path)


def test_document_without_tables_raises_parse_error(tmp_path):
    path = build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[])
    with pytest.raises(ParseError, match="표가 없다"):
        HwpxParser().parse(path)


def test_missing_file_raises_parse_error(tmp_path):
    with pytest.raises(ParseError):
        HwpxParser().parse(tmp_path / "없는파일.hwpx")


def test_missing_char_styles_fall_back_to_the_star_rule(tmp_path):
    """글자모양을 못 읽으면 `*`·`※` 규칙으로 내려간다(§3.5)."""
    title = [("제46회 개회식", "0"), ("*도 본청 셔틀 버스(17:00~)", "0")]
    rows = HEADER_ROW + simple_row(1, ["도지사", "18:30", title, "", "", ""])
    path = build_hwpx(tmp_path, header_text="【9월 7일 월요일】", tables=[table(rows, rows=2)])
    #  header.xml을 빼고 다시 묶는다
    stripped = tmp_path / "nostyle.hwpx"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(stripped, "w") as dst:
        for info in src.infolist():
            if info.filename != "Contents/header.xml":
                dst.writestr(info.filename, src.read(info.filename))
    parsed = HwpxParser().parse(stripped)
    assert parsed.items[0].title == "제46회 개회식"
    assert parsed.items[0].note == "*도 본청 셔틀 버스(17:00~)"
    assert any("글자모양" in w for w in parsed.warnings)


def test_load_char_styles_tolerates_a_bad_height():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(
        f'<hh:head {NS}><hh:charPr id="9" height="abc" textColor="#FF0000"/></hh:head>'
    )
    assert load_char_styles(root)["9"] == CharStyle(color="#FF0000", height=None)


def test_for_path_picks_the_hwpx_parser(tmp_path):
    assert isinstance(for_path(Path("a.hwpx")), HwpxParser)
    assert isinstance(for_path(Path("a.HWPX")), HwpxParser)
    assert isinstance(for_path(Path("a.json")), FixtureJsonParser)


# ------------------------------------------------------- 원본 대조 (샘플 필요)

#  PDF 픽스처와 hwpx 정답지가 **의도적으로** 다른 한 건.
#  PDF에는 글자 크기가 없어 `(여자 개인전 DB, 육성종목)`이 행사명에 붙어 있다.
#  hwpx는 작은 글씨를 보고 부기로 갈라낸다 — 이쪽이 옳다.
KNOWN_DELTA_TITLE_NORM = "전국장애인체육대회경기장현장참관및격려"


@requires_hwpx
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_hwpx_matches_the_answer_key(name):
    """정답지와 **필드 단위로** 일치해야 한다."""
    parsed = HwpxParser().parse(hwpx_raw(name))
    want = json.loads(hwpx_fixture(name).read_text(encoding="utf-8"))

    assert (parsed.file_date_month, parsed.file_date_day, parsed.file_weekday) == (
        want["file_date_month"], want["file_date_day"], want["file_weekday"]
    )
    assert parsed.warnings == want["warnings"] == []
    assert len(parsed.items) == len(want["items"])
    for got, expected in zip(parsed.items, want["items"]):
        assert asdict(got) == expected


@requires_hwpx
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_section_counts_match_the_design(name):
    """구분이 통째로 밀리면 여기서 걸린다 — 병합 해석이 틀렸을 때의 증상이다."""
    expected = {
        "0907": {"도지사": 1, "행정부지사": 2, "기후경제부지사": 4, "실 일정": 56},
        "0908": {"도지사": 5, "행정부지사": 4, "기후경제부지사": 4, "실 일정": 56},
        "0909": {"도지사": 4, "행정부지사": 4, "기후경제부지사": 4, "실 일정": 58},
        "0910": {"도지사": 2, "행정부지사": 1, "기후경제부지사": 3, "실 일정": 54},
    }[name]
    items = HwpxParser().parse(hwpx_raw(name)).items
    counts = {s: sum(1 for i in items if i.section == s) for s in expected}
    assert counts == expected


@requires_hwpx
@requires_fixtures
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_hwpx_and_pdf_agree_on_the_matching_keys(name):
    """완성 절차 2-2의 완료 기준.

    `(날짜, 구분, slot, title_norm)`이 같으면 매칭 결과가 같다. 딱 한 건만
    다르고, 그 한 건은 **PDF 쪽이 부기를 행사명에 붙여 둔 것**이다.
    """
    def keys(parsed):
        return {(e.date, e.section, e.slot, e.title_norm) for e in normalize(parsed, YEAR)[1]}

    from_hwpx = keys(HwpxParser().parse(hwpx_raw(name)))
    from_pdf = keys(FixtureJsonParser().parse(fixture(name)))

    only_hwpx = from_hwpx - from_pdf
    only_pdf = from_pdf - from_hwpx
    assert {k[3] for k in only_hwpx} == {KNOWN_DELTA_TITLE_NORM}
    assert {k[3] for k in only_pdf} == {KNOWN_DELTA_TITLE_NORM + "(여자개인전db,육성종목)"}
    assert len(only_hwpx) == len(only_pdf) == 1
    #  나머지는 전부 같다 — 62/68/69/59건
    assert len(from_hwpx & from_pdf) == len(from_hwpx) - 1


@requires_hwpx
def test_note_and_highlight_survive_normalization():
    """부기는 `CanonicalEvent.note`로 가고, 강조는 **해시에 들어가지 않는다**."""
    events = normalize(HwpxParser().parse(hwpx_raw("0907")), YEAR)[1]
    참관 = next(e for e in events if e.title_norm == KNOWN_DELTA_TITLE_NORM)
    assert 참관.note == "(여자 개인전 DB, 육성종목)"

    강조 = [e for e in events if e.highlight]
    assert len(강조) == 7
    #  같은 일정을 강조만 끄고 다시 만들어도 해시가 같아야 한다
    import dataclasses

    plain = dataclasses.replace(강조[0], highlight=False)
    assert plain.content_hash == 강조[0].content_hash
    assert plain.event_key == 강조[0].event_key


@requires_hwpx
def test_design_section_8_baseline_holds_from_real_hwpx(harness):
    """**가장 중요한 테스트.** §8 기준선을 hwpx 원본으로 그대로 재현한다.

    `test_sequence.py`는 PDF 픽스처로 같은 표를 지킨다. 둘 다 통과해야
    "파서를 갈아 끼워도 캘린더 결과가 같다"가 성립한다.
    """
    h = harness(parser=HwpxParser(), source=hwpx_raw)
    expected = [
        ("0907", 63, 0, 0, 0),
        ("0908", 23, 1, 1, 1),
        ("0909", 19, 1, 0, 0),
        ("0910", 13, 2, 3, 1),
    ]
    for name, created, overwritten, marked, llm_calls in expected:
        result = h.apply(name)
        totals = result.totals()
        assert result.outcomes[0].error is None
        assert (
            totals["created"],
            totals["updated"] + totals["restored"],
            totals["marked"],
            result.llm_calls,
        ) == (created, overwritten, marked, llm_calls), f"{name} 시나리오 불일치: {totals}"

    #  실계정 검증 기록과 같은 최종 상태: 118건 · `*` 4건
    summaries = h.summaries()
    assert len(summaries) == 118
    assert [s for s in summaries if s.startswith("* ")] == [
        "* 국무조정실 생명지킴추진본부장-행정부지사 면담",
        "* 신임 제주의료원장 임명장 수여식",
        "* 제46회 전국장애인체육대회 성화 출발식",
        "* 현안업무 점검회의(행정부지사주재)",
    ]


@requires_hwpx
def test_llm_sees_the_same_jibs_pair_from_hwpx(harness):
    """규칙 3에 넘어가는 쌍이 PDF 경로와 같아야 한다(§8의 0908)."""
    h = harness(parser=HwpxParser(), source=hwpx_raw)
    h.apply("0907")
    h.apply("0908")

    groups = h.resolver.seen_groups
    assert len(groups) == 1
    group = groups[0]
    assert (group.date, group.section) == (date(2026, 9, 10), Section.DEPT)
    assert [e.title for e in group.incoming] == [SAME_TITLES[0][0]]
