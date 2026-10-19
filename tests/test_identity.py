"""M1 -- identity detection and selection."""
from __future__ import annotations

import pytest

from fixture_repo import DANA, JEEVAN
from prism.identity import AmbiguousIdentity, UnknownIdentity, resolve


def test_detects_exactly_the_two_fixture_authors(identities):
    assert [i.email for i in identities] == [JEEVAN.email, DANA.email]


def test_commit_counts_are_exact(identities):
    by_email = {i.email: i for i in identities}
    assert by_email[JEEVAN.email].commit_count == 5
    assert by_email[DANA.email].commit_count == 1


def test_ordering_is_most_active_first(identities):
    counts = [i.commit_count for i in identities]
    assert counts == sorted(counts, reverse=True)


def test_date_range_matches_the_fixture(identities):
    jeevan = next(i for i in identities if i.email == JEEVAN.email)
    assert jeevan.first_at.startswith("2026-02-10")
    assert jeevan.last_at.startswith("2026-02-14")


def test_resolve_by_exact_email(identities):
    merged = resolve(identities, [JEEVAN.email])
    assert merged.primary_email == JEEVAN.email
    assert merged.commit_count == 5


def test_resolve_by_name(identities):
    merged = resolve(identities, ["Dana Okafor"])
    assert merged.primary_email == DANA.email


def test_resolve_is_case_insensitive(identities):
    merged = resolve(identities, [JEEVAN.email.upper()])
    assert merged.primary_email == JEEVAN.email


def test_merging_two_identities_sums_their_commits(identities):
    merged = resolve(identities, [JEEVAN.email, DANA.email])
    assert merged.commit_count == 6
    assert set(merged.emails) == {JEEVAN.email, DANA.email}
    # The primary is the more active address, so the Person node is stable.
    assert merged.primary_email == JEEVAN.email


def test_merged_identity_matches_every_member_address(identities):
    merged = resolve(identities, [JEEVAN.email, DANA.email])
    assert merged.matches(DANA.email.upper())
    assert not merged.matches("someone@else.com")


def test_unknown_selector_names_the_known_identities(identities):
    with pytest.raises(UnknownIdentity) as excinfo:
        resolve(identities, ["nobody@example.com"])
    assert JEEVAN.email in str(excinfo.value)


def test_ambiguous_selector_is_rejected_rather_than_guessed(identities):
    # A bare "a" substring-matches both fixture addresses.
    with pytest.raises(AmbiguousIdentity):
        resolve(identities, ["a"])


def test_exact_match_beats_substring_match(identities):
    # "dana@example.com" is also a substring of itself; an exact hit must not
    # be treated as ambiguous just because a looser tier would match too.
    merged = resolve(identities, [DANA.email])
    assert merged.primary_email == DANA.email


def test_handles_are_carried_through(jeevan):
    assert jeevan.handles == ("jeevansuresh",)
