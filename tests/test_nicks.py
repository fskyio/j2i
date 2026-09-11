"""Unit tests for IRC puppet nick allocation."""

import hashlib

from j2i.irc.nicks import (
    DEFAULT_NICK_LEN,
    allocate_puppet_nick,
    casemap_nick,
    collision_puppet_nick,
    preferred_puppet_nick,
    prepare_base_nick,
    puppet_identity,
    sanitize_irc_nick,
)


class TestPrepareBaseNick:
    def test_plain(self):
        assert prepare_base_nick("alice") == "alice"

    def test_leading_digit_gets_underscore(self):
        assert prepare_base_nick("2pac") == "_2pac"

    def test_illegal_then_leading_digit(self):
        # "9 lives!" -> sanitize "9-lives" -> "_9-lives"
        assert prepare_base_nick("9 lives!") == "_9-lives"


class TestPreferredPuppetNick:
    def test_default_suffix_and_pipe(self):
        assert preferred_puppet_nick("alice", "xmpp", "|", 30) == "alice|xmpp"

    def test_empty_suffix_is_unsuffixed(self):
        assert preferred_puppet_nick("alice", "", "|", 30) == "alice"

    def test_sanitizes_first(self):
        assert preferred_puppet_nick("a lice!", "xmpp", "|", 30) == "a-lice|xmpp"

    def test_truncates_base_to_fit_nicklen(self):
        # NICKLEN=16, "|xmpp" is 5 chars -> 11 of base ("SomethingVe")
        assert (
            preferred_puppet_nick("SomethingVeryLong", "xmpp", "|", 16)
            == "SomethingVe|xmpp"
        )

    def test_empty_suffix_clips_to_nicklen(self):
        assert preferred_puppet_nick("SomethingVeryLong", "", "|", 10) == "SomethingV"


class TestCollisionPuppetNick:
    def test_stable_for_same_identity(self):
        a = collision_puppet_nick("alice", "net\0alice", 30, "|")
        b = collision_puppet_nick("alice", "net\0alice", 30, "|")
        assert a == b
        ident = puppet_identity("net", "alice")
        digest = hashlib.sha256(ident.encode()).hexdigest()[:7]
        assert a == f"alice|{digest}"

    def test_fits_nicklen(self):
        nick = collision_puppet_nick("verylongusername", "id", 16, "|")
        assert len(nick) == 16
        assert nick.endswith(hashlib.sha256(b"id").hexdigest()[:7])


class TestAllocate:
    def test_prefers_open_nick(self):
        assert (
            allocate_puppet_nick(
                "alice",
                suffix="xmpp",
                separator="|",
                nicklen=30,
                taken=set(),
                identity="net\0alice",
            )
            == "alice|xmpp"
        )

    def test_hash_when_preferred_taken(self):
        taken = {casemap_nick("alice|xmpp")}
        got = allocate_puppet_nick(
            "alice",
            suffix="xmpp",
            separator="|",
            nicklen=30,
            taken=taken,
            identity="net\0alice",
        )
        assert got == collision_puppet_nick("alice", "net\0alice", 30, "|")

    def test_none_when_both_taken(self):
        preferred = preferred_puppet_nick("alice", "xmpp", "|", 30)
        fallback = collision_puppet_nick("alice", "net\0alice", 30, "|")
        taken = {casemap_nick(preferred), casemap_nick(fallback)}
        assert (
            allocate_puppet_nick(
                "alice",
                suffix="xmpp",
                separator="|",
                nicklen=30,
                taken=taken,
                identity="net\0alice",
            )
            is None
        )

    def test_rejects_casemapped_master_nick(self):
        taken = {casemap_nick("Alice|xmpp")}
        got = allocate_puppet_nick(
            "alice",
            suffix="xmpp",
            separator="|",
            nicklen=30,
            taken=taken,
            identity="net\0alice",
        )
        assert got != "alice|xmpp"


class TestCasemap:
    def test_ascii_lower(self):
        assert casemap_nick("Alice", "ascii") == "alice"

    def test_rfc1459_maps_pipe(self):
        # | and \ are equivalent under rfc1459
        assert casemap_nick("alice|x", "rfc1459") == casemap_nick("alice\\x", "rfc1459")


class TestSanitizeStillWorks:
    def test_plain(self):
        assert sanitize_irc_nick("alice") == "alice"

    def test_default_nick_len(self):
        assert DEFAULT_NICK_LEN == 30
