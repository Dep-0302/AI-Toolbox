---
name: "<img src=x onerror=alert(1)>"
description: "</script><script>alert('x')</script>"
license: unknown
---

This fixture proves the scanner treats metadata as inert data. Rendering must
still use textContent; the scanner does not convert this text into markup.
