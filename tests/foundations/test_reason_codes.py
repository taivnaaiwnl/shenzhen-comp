"""Reason-code catalogue (spec Appendix A)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from acb import reason_codes as rc

SPEC = Path(__file__).resolve().parents[2] / "docs" / "design" / "spec.md"


def appendix_a_rows() -> dict[int, tuple[str, str]]:
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("## Appendix A.", 1)[1].split("## Appendix B.", 1)[0]
    rows = {}
    for m in re.finditer(r"^\| (\d{4}) \| ([^|]+?) \| ([^|]+?) \|$", section, re.MULTILINE):
        rows[int(m.group(1))] = (m.group(2).strip(), m.group(3).strip())
    return rows


@pytest.mark.skipif(not SPEC.is_file(), reason="spec not present")
def test_catalogue_matches_appendix_a_verbatim():
    rows = appendix_a_rows()
    assert len(rows) >= 80
    for code, (name, text) in rows.items():
        if name == "(retired)":
            assert code in rc.RETIRED and rc.text(code) == text and rc.name(code) == "(retired)"
        else:
            assert rc.name(code) == name, code
            assert rc.text(code) == text, code
    assert set(rc.CATALOGUE) | set(rc.RETIRED) == set(rows)


def test_lookup_helpers():
    assert rc.text(2100) == "Refused: counterparty grade D"
    assert rc.name(2105) == "REFUSE_PROVENANCE_AMOUNT"
    assert rc.category(2100) == "decision_refuse"
    assert rc.category(1002) == "grade_adverse"
    assert rc.category(1103) == "grade_info"
    assert rc.category(2000) == "decision_ok"
    assert rc.category(2201) == "decision_approval"
    assert rc.category(2301) == "decision_info"
    assert rc.category(2400) == "refund"
    assert rc.category(3000) == "autonomy_promote"
    assert rc.category(3100) == "autonomy_demote"
    assert rc.category(3205) == "autonomy_freeze"
    assert rc.category(3300) == "autonomy_unfreeze"
    assert rc.category(4000) == "signer_ok"
    assert rc.category(4103) == "signer_reject"
    assert rc.category(4201) == "tx"
    assert rc.describe(2000) == "2000: Paid: grade A counterparty within tier limits"
    assert rc.texts([1006, 1007, 0]) == ("Few completed orders", "Identity registered recently")
    assert rc.ReasonCode.REFUSE_GRADE_D == 2100
    assert rc.ReasonCode.UNFREEZE_BY_OWNER == 3300


def test_retired_and_unused_codes():
    for code in (2006, 2116, 3201):
        assert not rc.is_known(code)
        assert rc.info(code).retired
        assert rc.category(code) == "retired"
    assert 2110 in rc.UNUSED
    with pytest.raises(KeyError):
        rc.text(2110)
    with pytest.raises(KeyError):
        rc.text(9999)


def test_every_code_fits_uint16_and_names_are_unique():
    names = [i.name for i in rc.CATALOGUE.values()]
    assert len(names) == len(set(names))
    assert all(0 < code < 2**16 for code in rc.CATALOGUE)
