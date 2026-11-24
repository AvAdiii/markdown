"""Mark links to other sites so that they open in a new tab."""
from __future__ import annotations

from . import Extension
from ..treeprocessors import Treeprocessor


class ExternalLinksTreeprocessor(Treeprocessor):
    def run(self, root):
        for a in root.iter('a'):
            href = a.get('href', '')
            if href.startswith(('http://', 'https://')):
                a.set('target', '_blank')
                a.set('rel', 'noopener noreferrer')


class ExternalLinksExtension(Extension):
    def extendMarkdown(self, md):
        md.treeprocessors.register(ExternalLinksTreeprocessor(md), 'external_links', 3)


def makeExtension(**kwargs):  # pragma: no cover
    return ExternalLinksExtension(**kwargs)
