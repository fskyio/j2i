"""Unit tests for pastebin service/field resolution (no network I/O)."""

from j2i.pastebin import _resolve_service


class TestResolveService:
    def test_known_service_uses_registered_url_and_field(self):
        assert _resolve_service("txt.t0.vc") == ("https://txt.t0.vc", "txt")

    def test_other_known_service(self):
        assert _resolve_service("kmi.aeza.net") == ("https://kmi.aeza.net", "kmi")

    def test_bare_host_gets_https_prefix_and_default_field(self):
        assert _resolve_service("paste.example.org") == (
            "https://paste.example.org",
            "txt",
        )

    def test_explicit_http_url_kept_as_is(self):
        assert _resolve_service("http://insecure.example") == (
            "http://insecure.example",
            "txt",
        )

    def test_explicit_https_url_kept_as_is(self):
        assert _resolve_service("https://paste.example.org/api") == (
            "https://paste.example.org/api",
            "txt",
        )

    def test_field_override_wins_over_default(self):
        _, field = _resolve_service("paste.example.org", field_override="content")
        assert field == "content"

    def test_field_override_wins_over_known_service(self):
        url, field = _resolve_service("txt.t0.vc", field_override="content")
        assert url == "https://txt.t0.vc"
        assert field == "content"
