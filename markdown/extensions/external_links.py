# Python Markdown

# A Python implementation of John Gruber's Markdown.

# Documentation: https://python-markdown.github.io/
# GitHub: https://github.com/Python-Markdown/markdown/
# PyPI: https://pypi.org/project/Markdown/

# Started by Manfred Stienstra (http://www.dwerg.net/).
# Maintained for a few years by Yuri Takhteyev (http://www.freewisdom.org).
# Currently maintained by Waylan Limberg (https://github.com/waylan),
# Dmitry Shachnev (https://github.com/mitya57) and Isaac Muse (https://github.com/facelessuser).

# Copyright 2007-2023 The Python Markdown Project (v. 1.7 and later)
# Copyright 2004, 2005, 2006 Yuri Takhteyev (v. 0.2-1.6b)
# Copyright 2004 Manfred Stienstra (the original version)

# License: BSD (see LICENSE.md for details).

"""
An extension to Python Markdown which adds `target="_blank"` and
`rel="noopener noreferrer"` to external links.
"""

from __future__ import annotations

from markdown.treeprocessors import Treeprocessor
from markdown.extensions import Extension


class ExternalLinksTreeprocessor(Treeprocessor):
    """ Add `target` and `rel` attributes to external `a` elements. """

    def run(self, root):
        for el in root.iter('a'):
            href = el.get('href')
            if isinstance(href, str) and href.startswith(('http://', 'https://')):
                el.set('target', '_blank')
                el.set('rel', 'noopener noreferrer')


class ExternalLinksExtension(Extension):
    """ Add the external links tree processor to Markdown. """

    def extendMarkdown(self, md):
        md.treeprocessors.register(ExternalLinksTreeprocessor(md), 'external_links', 15)


def makeExtension(**kwargs):  # pragma: no cover
    """ Return an instance of the `ExternalLinksExtension` """
    return ExternalLinksExtension(**kwargs)
