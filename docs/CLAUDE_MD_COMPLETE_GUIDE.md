# The Complete Guide to CLAUDE.md

> Everything you need to know about CLAUDE.md -- what it is, why it matters, how it works under the hood, and how to write one that maximizes Claude Code's potential.

---

## Table of Contents

1. [What is CLAUDE.md?](#what-is-claudemd)
2. [Why is CLAUDE.md Required?](#why-is-claudemd-required)
3. [Where Does It Come Into Picture?](#where-does-it-come-into-picture)
4. [File Locations and Hierarchy](#file-locations-and-hierarchy)
5. [How Claude Code Discovers and Loads CLAUDE.md](#how-claude-code-discovers-and-loads-claudemd)
6. [Context Window: What Gets Loaded and When](#context-window-what-gets-loaded-and-when)
7. [What to Include](#what-to-include)
8. [What NOT to Include](#what-not-to-include)
9. [Recommended Structure and Templates](#recommended-structure-and-templates)
10. [Advanced Features](#advanced-features)
11. [CLAUDE.md in Monorepos](#claudemd-in-monorepos)
12. [CLAUDE.md in CI/CD Pipelines](#claudemd-in-cicd-pipelines)
13. [CLAUDE.md vs Other AI Config Files](#claudemd-vs-other-ai-config-files)
14. [The Two Memory Systems: CLAUDE.md vs Auto Memory](#the-two-memory-systems)
15. [Best Practices (Distilled)](#best-practices-distilled)
16. [7 Common Mistakes to Avoid](#7-common-mistakes-to-avoid)
17. [7 Formatting Rules That Improve Adherence](#7-formatting-rules-that-improve-adherence)
18. [Real-World Case Studies and Metrics](#real-world-case-studies-and-metrics)
19. [Security Considerations](#security-considerations)
20. [Quick-Start Checklist](#quick-start-checklist)
21. [Sources and References](#sources-and-references)

---

## What is CLAUDE.md?

CLAUDE.md is a markdown file that Claude Code reads automatically at the **start of every conversation**. It acts as a persistent briefing document for your AI pair programmer.

Every Claude Code session begins with a **fresh context window** -- Claude has no memory of previous sessions. CLAUDE.md is the primary mechanism for carrying knowledge across sessions: project conventions, coding standards, architecture decisions, common commands, and critical gotchas.

Think of it as an onboarding document that runs every time Claude "shows up to work."

---

## Why is CLAUDE.md Required?

### The Statelessness Problem

Claude Code sessions are stateless. Without CLAUDE.md:

- Claude doesn't know your build commands (`bun run dev` vs `npm start` vs `make serve`)
- Claude doesn't know your project's architecture decisions
- Claude doesn't know your team's coding conventions
- Claude doesn't know the gotchas that would trip up any new developer
- Claude will make the same mistakes across sessions, with no way to learn

### The ROI

With a well-crafted CLAUDE.md:

| Metric | Improvement |
|--------|-------------|
| PRs merged per engineer per day | +67% (Anthropic internal) |
| Code written with Claude assistance | 70-90% (Anthropic internal) |
| Feature shipping speed | 2-3x faster (Sanity Engineering) |
| Net lines of code per week | +40% (350K LOC solo dev case study) |
| Research time reduction | ~80% (Anthropic inference team) |

CLAUDE.md is the **single highest-leverage configuration point** in Claude Code. A 100-line file that loads in every session shapes all of Claude's behavior in your project.

---

## Where Does It Come Into Picture?

CLAUDE.md comes into play at **every stage** of the Claude Code workflow:

```
Session Start
  |
  v
Claude Code loads system prompt (~4,200 tokens)
  |
  v
Loads auto memory (MEMORY.md)
  |
  v
Loads environment info (cwd, platform, git status)
  |
  v
Loads MCP tool names (deferred schemas)
  |
  v
Loads skill descriptions
  |
  v
*** Loads ~/.claude/CLAUDE.md (global preferences) ***
  |
  v
*** Loads project CLAUDE.md files (walking up from cwd) ***
  |
  v
*** Loads .claude/rules/ files (unconditional ones) ***
  |
  v
*** Loads CLAUDE.local.md files ***
  |
  v
User's first prompt arrives
  |
  v
During work: subdirectory CLAUDE.md and path-scoped rules load on-demand
  |
  v
On /compact: CLAUDE.md is re-read from disk (survives compaction)
```

### When CLAUDE.md Matters Most

- **Starting a new session**: Sets the foundation for all subsequent work
- **After compaction**: Re-injected fresh from disk, ensuring instructions persist
- **Navigating subdirectories**: Subdirectory CLAUDE.md files load lazily when Claude reads files there
- **In CI/CD**: Loaded automatically in GitHub Actions via `claude-code-action`
- **During PR review**: The `/review-pr` skill checks code against CLAUDE.md standards
- **With subagents**: Each subagent loads CLAUDE.md in its own context

---

## File Locations and Hierarchy

CLAUDE.md files can exist at multiple scopes, all of which are **concatenated** (not overridden):

| Scope | Location | Purpose | Shared With |
|-------|----------|---------|-------------|
| **Managed Policy** | macOS: `/Library/Application Support/ClaudeCode/CLAUDE.md` | Org-wide instructions from IT/DevOps | All users in org |
| | Linux/WSL: `/etc/claude-code/CLAUDE.md` | | |
| | Windows: `C:\Program Files\ClaudeCode\CLAUDE.md` | | |
| **User Global** | `~/.claude/CLAUDE.md` | Personal preferences across all projects | Just you |
| **User Rules** | `~/.claude/rules/*.md` | Personal topic-specific rules | Just you |
| **Project** | `./CLAUDE.md` or `./.claude/CLAUDE.md` | Team-shared project instructions | Team via git |
| **Project Rules** | `./.claude/rules/*.md` | Topic-specific, optionally path-scoped | Team via git |
| **Local** | `./CLAUDE.local.md` | Personal project-specific (gitignored) | Just you |
| **Subdirectory** | `./src/api/CLAUDE.md` | Directory-specific context | Team via git |

### Precedence Rules

Later-loaded files have **higher effective priority** due to recency bias in LLM attention:

1. Managed policy CLAUDE.md (lowest -- but **cannot be excluded**)
2. Managed policy rules
3. User-level CLAUDE.md
4. User-level rules
5. Project CLAUDE.md files (root to cwd, walking up)
6. Project `.claude/rules/` files
7. CLAUDE.local.md files (root to cwd) -- **highest effective priority**

---

## How Claude Code Discovers and Loads CLAUDE.md

### Directory Walking

Claude Code walks **up** the directory tree from your current working directory at launch. If you run Claude Code in `project/src/api/`, it loads:

```
project/CLAUDE.md
project/CLAUDE.local.md
project/src/CLAUDE.md          (if exists)
project/src/CLAUDE.local.md    (if exists)
project/src/api/CLAUDE.md      (if exists)
project/src/api/CLAUDE.local.md (if exists)
```

### Lazy Loading (Subdirectories)

CLAUDE.md files in **child directories below cwd** are NOT loaded at startup. They load **on-demand** when Claude reads files in those directories. This prevents context bloat in large projects.

### HTML Comment Stripping

Block-level HTML comments (`<!-- ... -->`) are **stripped** before injection into Claude's context, saving tokens. Use them for human-only notes:

```markdown
<!-- TODO: Update this section after the Q2 migration -->

# Build Commands
- `npm run dev` -- start dev server
```

### Compaction Survival

CLAUDE.md **fully survives compaction**. After `/compact`, Claude re-reads all CLAUDE.md files from disk and re-injects them fresh. This is a critical design choice -- your instructions persist even as conversation history is compressed to ~12% of original size.

---

## Context Window: What Gets Loaded and When

Based on the official context window visualization, the startup cost breakdown:

| Component | Approximate Tokens | Loaded When |
|-----------|-------------------|-------------|
| System prompt | ~4,200 | Always, first |
| Auto memory (MEMORY.md) | ~680 | Session start |
| Environment info | ~280 | Session start |
| MCP tool names (deferred) | ~120 | Session start |
| Skill descriptions | ~450 | Session start |
| ~/.claude/CLAUDE.md | ~320 | Session start |
| Project CLAUDE.md | ~1,800 (typical) | Session start |
| **Total startup overhead** | **~7,850+** | |

**Key insight**: Every line in CLAUDE.md costs tokens on **every single message**. A 200-line CLAUDE.md costs ~1,500-2,000 tokens. A bloated 500-line file burns 5,000+ tokens before you've typed anything.

### Technical Detail: System Prompt vs User Message

CLAUDE.md content is delivered as a **user message** after the system prompt, not as part of the system prompt itself. This is an architectural choice for caching economics -- the system prompt is shared across all users, while CLAUDE.md varies per project.

---

## What to Include

Focus on things Claude **cannot figure out** by reading the code:

| Include | Example |
|---------|---------|
| Build/test/dev commands | `bun run test:unit`, `make dev`, `npm run e2e` |
| Code style rules that differ from defaults | "Use ES modules, not CommonJS" |
| Testing conventions | "Use Vitest, not Jest. Run single tests, not full suite" |
| Repository etiquette | Branch naming, PR conventions, commit message format |
| Architecture decisions | "State management via Zustand in src/stores/" |
| Environment quirks | Required env vars, local setup dependencies |
| Common gotchas | "The API caches aggressively in .data/", "Never modify migrations/ directly" |
| Verification requirements | "Run tests and typecheck before committing" |
| Non-obvious domain knowledge | Business logic that isn't self-evident from code |

---

## What NOT to Include

| Exclude | Why |
|---------|-----|
| Anything Claude can figure out by reading code | Wastes tokens on obvious context |
| Standard language conventions | Claude already knows PEP 8, ESLint defaults, etc. |
| Detailed API documentation | Link to docs instead |
| Long explanations or tutorials | Use progressive disclosure |
| File-by-file codebase descriptions | Claude can explore on its own |
| Code style rules enforceable by linters | "Never send an LLM to do a linter's job" |
| Generic personality directives ("be a senior engineer") | No measurable effect |
| Code snippets that will become outdated | Use `file:line` references instead |
| Task-specific instructions | These distract on unrelated tasks; use skills instead |
| Information that changes frequently | Will become stale and misleading |

---

## Recommended Structure and Templates

### Minimal Template (~25 lines)

```markdown
# Project Name

Brief 1-2 sentence description.

# Commands
- `npm run dev` - start dev server
- `npm run test` - run tests
- `npm run build` - production build

# Code Conventions
- Use ES modules (import/export), not CommonJS
- Functional components with hooks

# Testing
- Use Vitest for unit tests
- Run single tests, not full suite

# Important
- IMPORTANT: Never modify migrations/ directly
- Run tests before committing
```

### Standard Template (~80 lines)

```markdown
# Project Name

What this project is and what it does (1-3 sentences).

# Commands
- `npm run dev` -- start dev server (port 3000)
- `npm run test` -- run all tests
- `npm run test -- path/to/file` -- run single test file
- `npm run build` -- production build
- `npm run lint` -- lint code
- `npm run typecheck` -- TypeScript type checking

# Architecture
- State management: Zustand (see src/stores/)
- API layer: src/api/handlers/
- Components: src/components/ (atomic design)
- Database: PostgreSQL via Prisma ORM

# Code Conventions
- Use ES modules (import/export), not CommonJS
- Destructure imports when possible
- Use functional components with hooks
- Error handling: use Result types, not try/catch for business logic
- Naming: camelCase for variables/functions, PascalCase for components/types

# Testing
- Framework: Vitest
- Run single tests for speed, not full suite
- New components require corresponding test file
- Integration tests hit real database, not mocks

# Git Workflow
- Branch naming: `feat/`, `fix/`, `refactor/` prefixes
- Conventional commits format
- Typecheck and test before committing
- PR descriptions must include "## Test Plan"

# Gotchas
- The API caches responses in .data/ -- clear it when debugging stale data
- IMPORTANT: Never modify files in migrations/ directly, use `npm run migrate:create`
- Environment variables must be in both .env and .env.example
```

### The Three-Dimension Framework

Structure your CLAUDE.md around three questions:

1. **WHAT** -- Tech stack, project structure, key dependencies
2. **WHY** -- Project purpose, business context, constraints
3. **HOW** -- How Claude should work: commands, conventions, verification steps

---

## Advanced Features

### @import Syntax

CLAUDE.md can import other files:

```markdown
See @README.md for project overview and @package.json for npm commands.

# Additional Context
- Git workflow: @docs/git-instructions.md
- API reference: @docs/api-design.md
- Personal overrides: @~/.claude/my-project-instructions.md
```

Rules:
- Relative paths resolve from the **containing file**, not cwd
- Recursive imports up to **5 hops** deep
- First-time external imports require approval

### .claude/rules/ Directory

Split instructions into topic-specific, optionally path-scoped files:

```
.claude/
  CLAUDE.md
  rules/
    code-style.md       # Always loaded
    testing.md           # Always loaded
    api-design.md        # Path-scoped (see below)
    security.md          # Always loaded
```

**Path-scoped rules** use YAML frontmatter:

```markdown
---
paths:
  - "src/api/**/*.ts"
  - "src/handlers/**/*.ts"
---

# API Development Rules
- All endpoints must include input validation
- Use zod schemas for request/response types
- Log all errors with structured metadata
```

These rules only load when Claude works with matching files, reducing context noise.

### Progressive Disclosure Pattern

Instead of stuffing everything into CLAUDE.md, use layers:

**Tier 1: CLAUDE.md (~500 tokens, always loaded)**
- Project overview, essential commands, pointers

**Tier 2: .claude/rules/ or docs/ (on-demand)**
- Topic-specific rules, loaded when relevant

**Tier 3: agent_docs/ (reference material)**
- Deep technical details, referenced by rules

```markdown
# CLAUDE.md

IMPORTANT: Before starting any task, identify which docs below are relevant and read them first:
- agent_docs/building_the_project.md -- Build and compile instructions
- agent_docs/running_tests.md -- Test execution and coverage
- agent_docs/code_conventions.md -- Code style and patterns
- agent_docs/service_architecture.md -- Service boundaries and data flow
```

### Hooks + CLAUDE.md: Two-Layer Governance

| Layer | Purpose | Enforcement |
|-------|---------|-------------|
| **CLAUDE.md** | Guidelines, advisory | Soft -- Claude may occasionally ignore |
| **Hooks** | Deterministic rules | Hard -- runs every time, no exceptions |

If Claude ignores a CLAUDE.md rule ~20% of the time, **move it to a hook**:

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Write|Edit",
      "hooks": [{
        "type": "command",
        "command": "npx prettier --write \"$CLAUDE_TOOL_INPUT_FILE_PATH\""
      }]
    }]
  }
}
```

### The /init Command

Run `/init` to generate a starter CLAUDE.md automatically. Claude analyzes your codebase and creates a file with build commands, test instructions, and conventions. If a CLAUDE.md already exists, `/init` suggests improvements.

Set `CLAUDE_CODE_NEW_INIT=1` for an interactive multi-phase flow.

---

## CLAUDE.md in Monorepos

### The Problem

A monorepo root CLAUDE.md can balloon to thousands of lines, with irrelevant context for most tasks.

### The Solution: Hierarchical Structure

```
CLAUDE.md                    (~60 lines -- shared architecture, universal commands)
  frontend/CLAUDE.md         (~80 lines -- React patterns, component guidelines)
  backend/CLAUDE.md          (~80 lines -- API conventions, DB patterns)
  core/CLAUDE.md             (~60 lines -- shared library conventions)
  mobile/CLAUDE.md           (~70 lines -- React Native specifics)
```

**Result**: One case study reduced their monorepo CLAUDE.md from 47,000 words to under 10,000 total across all files -- an 80% reduction with better performance.

### claudeMdExcludes

Skip irrelevant CLAUDE.md files from other teams:

```json
{
  "claudeMdExcludes": [
    "**/other-team/CLAUDE.md",
    "/home/user/monorepo/infra/.claude/rules/**"
  ]
}
```

Place in `.claude/settings.local.json` for machine-local exclusions.

### Additional Directories

The `--add-dir` flag gives Claude access to additional directories. Their CLAUDE.md files are NOT loaded by default. Enable with:

```bash
CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1 claude --add-dir ../shared-config
```

---

## CLAUDE.md in CI/CD Pipelines

### GitHub Actions

Anthropic provides `anthropics/claude-code-action@v1`, which runs Claude Code inside GitHub Actions. CLAUDE.md is automatically loaded during CI/CD runs.

### Workflow Patterns

1. **Automated PR review**: Claude reviews PRs against CLAUDE.md standards
2. **Issue-to-PR automation**: Claude reads issues, creates implementation PRs
3. **Test generation**: Claude generates tests for changed files
4. **Code review with compliance checking**: `/review-pr` checks against CLAUDE.md
5. **Automated fix suggestions**: Claude proposes fixes for failing CI

### CI-Specific CLAUDE.md Content

```markdown
# CI/CD Notes
- IMPORTANT: In CI mode, never prompt for user input
- Run full test suite, not single tests
- Security: never output env vars or secrets in logs
- Fail fast on type errors
```

---

## CLAUDE.md vs Other AI Config Files

| Feature | CLAUDE.md | .cursorrules | copilot-instructions.md | AGENTS.md |
|---------|-----------|-------------|------------------------|-----------|
| **Tool** | Claude Code | Cursor | GitHub Copilot | Universal |
| **Location** | Root + hierarchy + `~/.claude/` | Root / `.cursor/rules/` | `.github/` | Root + subdirs |
| **Format** | Markdown | MDC (Markdown + frontmatter) | Markdown + frontmatter | Markdown |
| **Directory walking** | Yes (full hierarchy) | No (flat) | No | Yes |
| **@imports** | Yes (5 levels) | Via `@filename` reference | No | No |
| **Path scoping** | Via `.claude/rules/` | Via globs frontmatter | Via instructions/*.md | Via directory placement |
| **Auto memory** | Yes | No | No | No |
| **Lazy loading** | Yes (subdirs on-demand) | No | No | No |
| **Recommended size** | 200 lines | 500 lines | Not specified | Not specified |
| **Adoption** | Claude Code only | Cursor only | GitHub Copilot | 60,000+ repos |

### Cross-Tool Strategy

If your team uses multiple AI tools:

```markdown
# CLAUDE.md
@AGENTS.md

## Claude Code Specific
- Use plan mode for changes under src/billing/
- Run verification with `bun test` before completing tasks
```

The `rule-porter` tool can convert between formats.

---

## The Two Memory Systems

| Aspect | CLAUDE.md | Auto Memory (MEMORY.md) |
|--------|-----------|------------------------|
| **Who writes it** | You (the developer) | Claude (automatically) |
| **Content** | Instructions, rules, standards | Learnings, patterns, build commands |
| **Scope** | Project, user, or org | Per working tree |
| **Token limit** | No hard limit (full file loaded) | First 200 lines or 25KB |
| **Storage** | In project or home directory | `~/.claude/projects/<hash>/memory/` |
| **Survives compaction** | Yes | Yes |
| **Best for** | Intentional guidance you want enforced | Organic learnings Claude discovers |

Use the `/memory` command to view all loaded instruction files and manage auto memory.

---

## Best Practices (Distilled)

### The Golden Rule: Keep It Concise

- **Target**: Under 200 lines per file, under 100 is even better
- **Why**: Frontier LLMs reliably follow ~150-200 instructions. Claude Code's system prompt already uses ~50 of those. Past 80 project-specific instructions, adherence drops.
- **Boris Cherny (Claude Code creator)** uses ~100 lines (~2,500 tokens). It outperforms 500-1,000 line alternatives.

### The Verification Loop

> "Probably the most important thing to get great results" -- Boris Cherny

Give Claude a way to **verify its own work**:

```markdown
# Verification
- Run `npm run typecheck` after code changes
- Run `npm test -- --related` before completing tasks
- Check browser output for UI changes
```

This alone yields a **2-3x quality improvement**.

### Living Document

- Update CLAUDE.md **multiple times per week** (Anthropic's own teams do this)
- When Claude makes a mistake, add a rule to prevent it recurring
- Make CLAUDE.md updates part of the PR process
- Schedule monthly audits to prune stale rules

### Emphasis for Critical Rules

Use "IMPORTANT" or "YOU MUST" sparingly for critical instructions:

```markdown
- IMPORTANT: Never force push to main
- YOU MUST run tests before committing
```

Overuse dilutes the effect.

---

## 7 Common Mistakes to Avoid

Based on analysis of Boris Cherny's workflow and community patterns:

| # | Mistake | Fix |
|---|---------|-----|
| 1 | **Context Stuffing** -- 10,000+ tokens of edge cases | Prune ruthlessly to <200 lines |
| 2 | **Static Documentation** -- write once, never update | Monthly audits, update on every mistake |
| 3 | **Solo Configuration** -- keeping it gitignored | Check into git, use CLAUDE.local.md for personal stuff |
| 4 | **Skipping Plan Mode** -- jumping straight to auto-accept | Plan first for complex tasks (3+ steps) |
| 5 | **Missing Verification Loop** -- no tests or checks | Add explicit "run tests, typecheck, verify" |
| 6 | **Dangerous Permissions** -- `--dangerously-skip-permissions` | Use `/permissions` to pre-allow safe commands |
| 7 | **Format Drift** -- no auto-formatting enforcement | Use PostToolUse hooks to auto-format after every edit |

---

## 7 Formatting Rules That Improve Adherence

1. **Include rationale with rules** -- "Never force push -- rewrites shared history, unrecoverable for collaborators" is followed more reliably than bare "Never force push"

2. **Keep heading hierarchy shallow** -- H1 for title, H2 for sections, H3 max. Deep nesting dilutes attention.

3. **Name imported files descriptively** -- `deployment-checklist.md` not `notes.md`

4. **Use headers as structural anchors** -- Claude scans headers like a table of contents

5. **Put commands in code blocks** -- "A command in a code fence is a command. A command in a sentence is a suggestion."

6. **Use standard section names** -- Testing, Commands, Structure, Conventions, Boundaries (leverages training data familiarity)

7. **Make instructions actionable** -- "Format with `ruff format` before committing" beats "maintain code quality"

---

## Real-World Case Studies and Metrics

### Anthropic Internal

- **67% increase** in PRs merged per engineer per day
- **70-90%** of code now written with Claude Code assistance
- Security team: stack trace analysis **3x faster**
- Inference team: research time reduced by **~80%**
- Data infra: **~20 minutes saved** per Kubernetes outage

### 350K LOC Solo Developer

- Before: 2,506 net lines of code per week
- After: 5,947 net lines per week (**+40%**)
- Test code: jumped from 505 to 2,043 net LOC per week
- Used hierarchical nested CLAUDE.md with "HARD RULES" sections

### Sanity Engineering

- AI generates 80% of initial code implementations
- Features ship **2-3x faster**
- Monthly cost: $1,000-1,500 per senior engineer
- "Three-attempt framework" -- first attempt is 95% garbage, third is workable

### TELUS (Enterprise Scale)

- **500,000+ staff hours saved**
- 47 enterprise-grade apps delivered
- **$90M+** in measurable business benefit
- 30% improvement in code delivery velocity
- Processing **100B+ tokens/month** across 57,000 team members

---

## Security Considerations

### CLAUDE.md as an Attack Vector

CLAUDE.md files from untrusted sources (PRs, forks) are a **prompt injection attack surface**. A malicious CLAUDE.md could instruct Claude to exfiltrate data or execute arbitrary commands.

### Mitigations

- **Always review** CLAUDE.md changes in PRs
- First-time codebase runs require **trust verification**
- The permission system requires explicit approval for sensitive operations
- Network request approval is required by default
- Use **managed policy CLAUDE.md** for org-wide security requirements
- Use `permissions.deny` in settings.json for blocking file access (more reliable than `.claudeignore`)

### Enterprise Controls

Organizations can deploy managed CLAUDE.md at system paths that **cannot be excluded**:

```
macOS:     /Library/Application Support/ClaudeCode/CLAUDE.md
Linux/WSL: /etc/claude-code/CLAUDE.md
Windows:   C:\Program Files\ClaudeCode\CLAUDE.md
```

---

## Quick-Start Checklist

1. Run `/init` in your project to generate a starter CLAUDE.md
2. **Prune** to under 100 lines -- focus on what Claude would get wrong without guidance
3. Add **verification requirements** (tests, typecheck, lint)
4. Move task-specific knowledge to `.claude/skills/` or `.claude/rules/`
5. Set up **PostToolUse hooks** for auto-formatting
6. **Check CLAUDE.md into git** -- the team should contribute to it
7. Create **CLAUDE.local.md** (gitignored) for personal preferences
8. **Review and prune monthly**
9. When Claude makes a recurring mistake, add a concise rule **with rationale**
10. Use **plan mode** for complex tasks before jumping into implementation

---

## Sources and References

### Official Documentation
- [How Claude Remembers Your Project](https://code.claude.com/docs/en/memory)
- [Best Practices for Claude Code](https://code.claude.com/docs/en/best-practices)
- [Explore the Context Window](https://code.claude.com/docs/en/context-window)
- [Claude Code Settings](https://code.claude.com/docs/en/settings)
- [The .claude Directory](https://code.claude.com/docs/en/claude-directory)
- [Claude Code Hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code Security](https://code.claude.com/docs/en/security)
- [Claude Code GitHub Actions](https://code.claude.com/docs/en/github-actions)
- [Using CLAUDE.md Files (Blog)](https://claude.com/blog/using-claude-md-files)

### Community Guides
- [Writing a Good CLAUDE.md -- HumanLayer](https://www.humanlayer.dev/blog/writing-a-good-claude-md)
- [CLAUDE.md Best Practices (10 Sections) -- UX Planet](https://uxplanet.org/claude-md-best-practices-1ef4f861ce7c)
- [Stop Bloating Your CLAUDE.md -- alexop.dev](https://alexop.dev/posts/stop-bloating-your-claude-md-progressive-disclosure-ai-coding-tools/)
- [7 Formatting Rules for the Machine -- DEV Community](https://dev.to/cleverhoods/-claudemd-best-practices-7-formatting-rules-for-the-machine-3d3l)
- [How to Write a Good CLAUDE.md -- Builder.io](https://www.builder.io/blog/claude-md-guide)
- [Writing the Best CLAUDE.md -- DataCamp](https://www.datacamp.com/tutorial/writing-the-best-claude-md)
- [Claude Code in Monorepos -- DEV Community](https://dev.to/myougatheaxo/claude-code-in-monorepos-hierarchical-claudemd-and-package-scoped-instructions-1il9)

### Case Studies
- [How Anthropic Teams Use Claude Code](https://claude.com/blog/how-anthropic-teams-use-claude-code)
- [TELUS Boosts Workplace Innovation](https://claude.com/customers/telus)
- [Claude Code in Production: 40% Productivity Increase](https://dev.to/dzianiskarviha/integrating-claude-code-into-production-workflows-lbn)
- [First Attempt Will Be 95% Garbage -- Sanity](https://www.sanity.io/blog/first-attempt-will-be-95-garbage)

### GitHub Resources
- [awesome-claude-md Collection](https://github.com/josix/awesome-claude-md)
- [claude-code-best-practice](https://github.com/shanraisshan/claude-code-best-practice)
- [claude-md-templates](https://github.com/abhishekray07/claude-md-templates)
- [anthropics/claude-code](https://github.com/anthropics/claude-code)
- [anthropics/claude-code-action](https://github.com/anthropics/claude-code-action)

### Technical Analysis
- [Boris Cherny's Workflow Thread](https://x.com/bcherny/status/2007179832300581177)
- [Inside Claude Code Architecture -- Penligent](https://www.penligent.ai/hackinglabs/inside-claude-code-the-architecture-behind-tools-memory-hooks-and-mcp/)
- [Claude Code Compaction Explained](https://okhlopkov.com/claude-code-compaction-explained/)
- [Measuring Claude Code ROI -- Faros AI](https://www.faros.ai/blog/how-to-measure-claude-code-roi-developer-productivity-insights-with-faros-ai)
