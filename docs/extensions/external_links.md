---
title: External Links Extension
---

# External Links

## Summary

The External Links extension makes external links open in a new browsing
context. Every external link gets `target="_blank"` and
`rel="noopener noreferrer"`.

A link is external when its `href` starts with `http://` or `https://`.
Relative links, `#anchors` and `mailto:` links are not changed, and neither are
images. Automatic links such as `<https://example.com>` are included.

Nothing changes unless the extension is enabled.

## Usage

See [Extensions](index.md) for general extension usage. Use `external_links`
as the name of the extension.

```python
import markdown
html = markdown.markdown(text, extensions=['external_links'])
```

The extension has no configuration options.
