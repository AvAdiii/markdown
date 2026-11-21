"""Tests for the external_links extension."""

import markdown
from markdown.test_tools import TestCase

EXT = ['external_links']


class TestExternalLinks(TestCase):

    def test_ac_fr_001_1_loads_by_name(self):
        markdown.markdown('[a](https://example.com)', extensions=EXT)

    def test_ac_fr_001_2_plain_text_unchanged(self):
        md = markdown.Markdown(extensions=EXT)
        self.assertEqual(md.convert('text'), '<p>text</p>')

    def test_ac_fr_002_1_disabled_inline_link(self):
        self.assertMarkdownRenders(
            '[a](https://example.com)',
            '<p><a href="https://example.com">a</a></p>'
        )

    def test_ac_fr_002_2_disabled_autolink(self):
        out = markdown.markdown('<https://example.com>')
        self.assertNotIn('target=', out)
        self.assertNotIn('rel=', out)

    def test_ac_fr_003_1_https_inline_link(self):
        out = markdown.markdown('[a](https://example.com)', extensions=EXT)
        self.assertEqual(out.count('<a '), 1)
        self.assertIn('href="https://example.com"', out)
        self.assertIn('target="_blank"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_ac_fr_003_2_http_link_with_query(self):
        out = markdown.markdown('[a](http://example.com/x?y=1)', extensions=EXT)
        self.assertIn('target="_blank"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_ac_fr_003_3_text_and_href_unchanged(self):
        out = markdown.markdown('[a](https://example.com)', extensions=EXT)
        self.assertIn('href="https://example.com"', out)
        self.assertIn('>a</a>', out)

    def test_ac_fr_004_1_autolink(self):
        out = markdown.markdown('<https://example.com>', extensions=EXT)
        self.assertEqual(out.count('<a '), 1)
        self.assertIn('href="https://example.com"', out)
        self.assertIn('target="_blank"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_ac_fr_004_2_mailto_autolinks(self):
        for src in ('<mailto:a@example.com>', '<a@example.com>'):
            out = markdown.markdown(src, extensions=EXT)
            self.assertIn('<a ', out)
            self.assertNotIn('target=', out)
            self.assertNotIn('rel=', out)

    def test_ac_fr_005_1_reference_link(self):
        out = markdown.markdown('[a][1]\n\n[1]: https://example.com', extensions=EXT)
        self.assertEqual(out.count('<a '), 1)
        self.assertIn('target="_blank"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_ac_fr_006_1_relative_absolute_path(self):
        self.assertMarkdownRenders(
            '[a](/docs/page.html)',
            '<p><a href="/docs/page.html">a</a></p>',
            extensions=EXT
        )

    def test_ac_fr_006_2_anchor(self):
        self.assertMarkdownRenders(
            '[a](#section)',
            '<p><a href="#section">a</a></p>',
            extensions=EXT
        )

    def test_ac_fr_006_3_mailto_link(self):
        out = markdown.markdown('[a](mailto:a@example.com)', extensions=EXT)
        self.assertIn('<a ', out)
        self.assertNotIn('target=', out)
        self.assertNotIn('rel=', out)

    def test_ac_fr_006_4_relative_file(self):
        self.assertMarkdownRenders(
            '[a](page.html)',
            '<p><a href="page.html">a</a></p>',
            extensions=EXT
        )

    def test_ac_fr_007_1_image_only(self):
        self.assertMarkdownRenders(
            '![alt](https://example.com/i.png)',
            '<p><img alt="alt" src="https://example.com/i.png" /></p>',
            extensions=EXT
        )

    def test_ac_fr_007_2_linked_image(self):
        out = markdown.markdown(
            '[![alt](https://example.com/i.png)](https://example.com)', extensions=EXT
        )
        start = out.index('<a ')
        a_tag = out[start:out.index('>', start)]
        self.assertIn('target="_blank"', a_tag)
        self.assertIn('rel="noopener noreferrer"', a_tag)
        istart = out.index('<img')
        img_tag = out[istart:out.index('>', istart)]
        self.assertNotIn('target=', img_tag)
        self.assertNotIn('rel=', img_tag)

    def test_ac_fr_008_1_no_config_needed(self):
        out = markdown.markdown('[a](https://example.com)', extensions=EXT)
        self.assertEqual(out.count('<a '), 1)
        self.assertIn('href="https://example.com"', out)
        self.assertIn('target="_blank"', out)
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertIn('>a</a>', out)
