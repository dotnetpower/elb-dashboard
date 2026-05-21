---
title: Glassmorphic UI Rules
description: Design tokens, glass-card CSS, motion budget, and WCAG AA accessibility rules for the ElasticBLAST Control Plane SPA.
---

# Glassmorphic UI — Design Rules (detail)

> Extracted from `.github/copilot-instructions.md` §10 on 2026-05-19.

Calm, muted, low-contrast surfaces. Reference tokens (use as CSS variables in `web/src/theme/`):

```css
:root {
  --glass-bg: rgba(255, 255, 255, 0.08);
  --glass-bg-strong: rgba(255, 255, 255, 0.14);
  --glass-border: rgba(255, 255, 255, 0.18);
  --glass-blur: 18px;
  --glass-radius: 16px;
  --bg-gradient: radial-gradient(1200px 600px at 20% 0%, #1c2541 0%, #0b132b 60%, #050816 100%);
  --text-primary: #e8ecf4;
  --text-muted:   #9aa3b8;
  --accent:       #7aa7ff;   /* cool, low-saturation blue */
  --success:      #6ad6a3;
  --warning:      #f0c674;
  --danger:       #e07b8a;
}

.glass-card {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: var(--glass-radius);
  backdrop-filter: blur(var(--glass-blur));
  -webkit-backdrop-filter: blur(var(--glass-blur));
  box-shadow: 0 8px 32px rgba(0,0,0,0.25);
}
```

* Avoid pure black, pure white, and saturated brand colors. Stay in the deep-navy / cool-grey family.
* No drop shadows above 32 px blur, no neon, no animated gradients.
* Motion: `prefers-reduced-motion` respected; transitions ≤ 200 ms ease-out.
* Iconography: `lucide-react`, stroke 1.5.
* Components must be readable on a 1366×768 laptop and accessible (WCAG AA contrast on text against the glass surface).
