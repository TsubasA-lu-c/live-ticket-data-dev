---
name: project-change
description: Modify Dev live-ticket delivery data or artist relation controls. Use for reviewed data changes, Dev promotion, relation updates, or validation; do not use for read-only inspection.
---

1. Read `README.md` and only the files relevant to the requested change.
2. Treat Dev and production as separate gates.
3. Preserve unrelated IDs and existing delivery data; never invent missing facts.
4. For relation changes, preserve the control-file contract and keep Apple Music data artist-specific.
5. Use the established collector/materialize/validation workflow when generated delivery data is affected.
6. Report changed files, validation performed, and whether the change is Dev-only or proposed for production.
