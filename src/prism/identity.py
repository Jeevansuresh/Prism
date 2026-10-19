"""Stage 1 -- who committed to this repository.

A person is not one email. The same human shows up as
``jeevansuresh <jeevansuresh2508@gmail.com>`` on a laptop and
``Jeevan Suresh <admin@work.com>`` on a work machine, and a portfolio that
treats those as two people is wrong in the one way that matters.

So this module keeps two levels: a raw :class:`Identity` per email address as
git records it, and a :class:`MergedIdentity` that represents the human the
caller selected -- possibly several emails, deliberately chosen rather than
guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .gitio import UNIT, git_lines

__all__ = [
    "Identity", "MergedIdentity", "detect_identities", "resolve",
    "AmbiguousIdentity", "UnknownIdentity",
]


class IdentityError(ValueError):
    """Base class for identity selection failures."""


class UnknownIdentity(IdentityError):
    """No identity in the repository matched the selector."""


class AmbiguousIdentity(IdentityError):
    """The selector matched more than one identity."""


@dataclass(frozen=True)
class Identity:
    """One author email as it appears in git history."""

    email: str
    names: tuple[str, ...]
    commit_count: int
    first_at: str
    last_at: str

    @property
    def display_name(self) -> str:
        return self.names[0] if self.names else self.email

    def __str__(self) -> str:
        return f"{self.display_name} <{self.email}>"


@dataclass(frozen=True)
class MergedIdentity:
    """The human a build is about -- one or more :class:`Identity` records."""

    primary_email: str
    emails: tuple[str, ...]
    names: tuple[str, ...]
    commit_count: int
    first_at: str
    last_at: str
    handles: tuple[str, ...] = field(default=())

    @property
    def display_name(self) -> str:
        return self.names[0] if self.names else self.primary_email

    def matches(self, email: str) -> bool:
        return email.strip().casefold() in self.emails

    def __str__(self) -> str:
        return f"{self.display_name} <{self.primary_email}>"


def detect_identities(repo: Path | str, *, include_merges: bool = False) -> list[Identity]:
    """Every distinct author email in the repository, most active first.

    Ordering is ``commit_count`` descending then email ascending, so the list
    is stable across runs and the most likely selection is first.
    """
    args = [
        "log",
        "--all",
        "--format=%an" + UNIT + "%ae" + UNIT + "%aI",
    ]
    if not include_merges:
        args.insert(2, "--no-merges")

    rows = git_lines(repo, *args, check=False)

    # email -> {names (ordered by frequency), count, first, last}
    buckets: dict[str, dict] = {}
    for row in rows:
        parts = row.split(UNIT)
        if len(parts) != 3:
            continue
        name, email, when = (p.strip() for p in parts)
        if not email:
            continue
        key = email.casefold()
        bucket = buckets.setdefault(
            key, {"names": {}, "count": 0, "first": when, "last": when}
        )
        bucket["count"] += 1
        if name:
            bucket["names"][name] = bucket["names"].get(name, 0) + 1
        # git log is newest-first, so every subsequent row is older.
        if when and when < bucket["first"]:
            bucket["first"] = when
        if when and when > bucket["last"]:
            bucket["last"] = when

    identities = [
        Identity(
            email=email,
            names=tuple(
                sorted(data["names"], key=lambda n: (-data["names"][n], n))
            ),
            commit_count=data["count"],
            first_at=data["first"],
            last_at=data["last"],
        )
        for email, data in buckets.items()
    ]
    identities.sort(key=lambda i: (-i.commit_count, i.email))
    return identities


def _candidates(identities: list[Identity], selector: str) -> list[Identity]:
    """Match a selector against identities, most specific match wins.

    Tried in order: exact email, exact name, then substring of either. The
    first tier that produces any match is the answer -- a substring match never
    competes with an exact one.
    """
    needle = selector.strip().casefold()
    if not needle:
        return []

    exact_email = [i for i in identities if i.email.casefold() == needle]
    if exact_email:
        return exact_email

    exact_name = [i for i in identities if any(n.casefold() == needle for n in i.names)]
    if exact_name:
        return exact_name

    return [
        i
        for i in identities
        if needle in i.email.casefold() or any(needle in n.casefold() for n in i.names)
    ]


def resolve(
    identities: list[Identity],
    selectors: list[str],
    *,
    handles: tuple[str, ...] = (),
) -> MergedIdentity:
    """Resolve one or more selectors into the single person a build is about.

    Each selector must match exactly one identity; several selectors merge into
    one :class:`MergedIdentity`. The primary email is the matched address with
    the most commits -- ties broken alphabetically, so the result is stable.
    """
    if not selectors:
        raise UnknownIdentity("no author selector given")

    matched: dict[str, Identity] = {}
    for selector in selectors:
        found = _candidates(identities, selector)
        if not found:
            known = ", ".join(str(i) for i in identities[:10]) or "<none>"
            raise UnknownIdentity(
                f"no author matching {selector!r}. Known identities: {known}"
            )
        if len(found) > 1:
            listed = ", ".join(str(i) for i in found)
            raise AmbiguousIdentity(
                f"{selector!r} matches {len(found)} identities: {listed}. "
                f"Use the full email address."
            )
        identity = found[0]
        matched[identity.email.casefold()] = identity

    members = sorted(matched.values(), key=lambda i: (-i.commit_count, i.email))
    primary = members[0]

    names: list[str] = []
    for member in members:
        for name in member.names:
            if name not in names:
                names.append(name)

    return MergedIdentity(
        primary_email=primary.email.casefold(),
        emails=tuple(sorted(m.email.casefold() for m in members)),
        names=tuple(names),
        commit_count=sum(m.commit_count for m in members),
        first_at=min(m.first_at for m in members if m.first_at) if members else "",
        last_at=max(m.last_at for m in members if m.last_at) if members else "",
        handles=tuple(handles),
    )
