"""Tests for the external_links extension."""

from markdown.test_tools import TestCase
from markdown.extensions.external_links import ExternalLinksExtension


class TestExternalLinks(TestCase):

    def test_external_link(self):
        self.assertMarkdownRenders(
            '[docs](https://example.com)',
            '<p><a href="https://example.com" rel="noopener noreferrer" target="_blank">docs</a></p>',
            extensions=[ExternalLinksExtension()]
        )

    def test_relative_link_untouched(self):
        self.assertMarkdownRenders(
            '[a](/docs/)',
            '<p><a href="/docs/">a</a></p>',
            extensions=[ExternalLinksExtension()]
        )
