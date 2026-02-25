---
name: document-that
description: Publish or update operational documentation in the JamBetterBase Git repository and provide exact retrieval instructions for humans. Use when a user asks to “document this,” “update the runbook,” “put docs on git,” “share where to find it,” or requests a copy/paste command to open docs from SSH.
---

# Document That

## Overview
Create clear operator-facing documentation files, commit them to Git, push to `origin/master`, and return direct commands/URLs so a human can retrieve the docs immediately.

## Workflow

1. Identify the target doc path and audience (operator vs developer).
2. Write/update the document with exact commands and examples.
3. Verify file readability from SSH (`cat <path>`).
4. Commit and push documentation changes to the repository.
5. Return:
   - local path
   - git branch/commit
   - GitHub URL (if available)
   - one-line retrieval command(s)

## Required Output Checklist

Always include these in the final response:

- **Doc path:** absolute path on server.
- **Git repo:** `https://github.com/rjbuckley332/JamBetterBase.git`
- **Branch:** `master`
- **Commit SHA:** short SHA after push.
- **How to read from SSH:**
  ```bash
  cat <absolute-doc-path>
  ```
- **How to pull latest docs locally:**
  ```bash
  cd /home/nds
  git pull origin master
  ```

## Quality Standard

- Keep docs concise but operationally complete.
- Prefer executable command blocks over prose.
- Include rollback commands where relevant.
- Avoid ambiguous placeholders unless explicitly marked `REPLACE_ME`.
