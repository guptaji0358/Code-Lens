---
name: project-memory
description: Initialize, audit, and incrementally maintain project memory for any codebase. Use when onboarding to a repository, refreshing stale memory, or updating memory after implementation changes.
---

# Project Memory

## Purpose

Maintain a reliable, concise, project-specific memory system that helps
future Claude Code sessions understand an existing codebase without
repeatedly reading the entire repository.

This skill is project-independent. Do not assume a language, framework,
platform, architecture, repository size, or application type.

Core operating sequence:

1.  Read project instructions and existing memory.
2.  Inspect only the source code needed to verify or extend
    understanding.
3.  Perform the requested memory operation or implementation task.
4.  Update only the memory affected by verified changes.
5.  Validate links, accuracy, and the Markdown line limit.

Memory is a navigation and understanding aid. The current source code
and configuration remain authoritative.

## When to use this skill

Use this skill when the user asks to:

-   Initialize project memory for the current repository.
-   Understand or audit an existing project-memory system.
-   Refresh memory that may be stale.
-   Continue project work where the memory-first workflow is relevant.
-   Update project memory after meaningful implementation changes.

If the user asks for an ordinary code change and this skill is not
active, follow the repository's `CLAUDE.md` instructions. The generated
linker should carry the essential memory-first and memory-maintenance
rules into later sessions.

## Operating modes

Choose the mode that matches the request:

-   **Initialize:** No usable memory system exists; inspect the project
    and create one.
-   **Refresh:** Memory exists; verify it against relevant code and
    update stale or missing information.
-   **Audit:** Check memory quality, references, duplication, and line
    limits; fix issues when requested or clearly part of the task.
-   **Maintain:** Update memory affected by the current implementation
    task without re-documenting the whole repository.

Do not run a full initialization when a focused refresh or maintenance
operation is sufficient.

## 1. Discover before inspecting source

Start at the repository root.

1.  Read the root `CLAUDE.md`, if present.
2.  Identify the existing memory location and read its index first.
3.  Read only memory files relevant to the current request.
4.  Inspect other project instructions that apply to the files being
    changed.
5.  Check the repository structure and relevant configuration files.
6.  Inspect relevant source files to verify claims and understand
    implementation.

Check for an existing memory convention before creating anything. Reuse
an established project memory location and structure when it is clearly
intended for this purpose. Do not create a competing memory system.

Default location when no suitable system exists:

`.claude/memory/`

Do not move, delete, or overwrite unrelated documentation.

## 2. Memory-first investigation

Treat existing memory as a map, not as unquestionable truth.

-   Use the index to select relevant memory files.
-   Follow references to relevant modules, interfaces, configuration,
    and tests.
-   Verify important claims against the current implementation.
-   Expand inspection only when the task requires it or memory is
    insufficient.
-   Do not recursively read every source file as a default onboarding
    strategy.
-   Do not claim to understand uninspected areas.
-   When memory conflicts with source code, trust the current source and
    correct the memory.
-   When evidence is incomplete, label the point as unknown or
    unverified rather than guessing.

For initialization, inspect enough of the repository to produce a useful
architectural map. Prioritize entry points, build and package
configuration, module boundaries, public interfaces, data flow, tests,
and development workflows where applicable.

## 3. Design a project-specific memory structure

Create multiple focused Markdown files rather than one monolithic
document.

Always create `index.md` as the navigation entry point.

Choose other files according to actual project complexity. Possible
topics include:

-   Project purpose, scope, and capabilities.
-   Architecture and component relationships.
-   Directory and module responsibilities.
-   Technology stack and dependencies.
-   Data flow and execution lifecycle.
-   Public interfaces and integration boundaries.
-   Build, run, test, and release workflows.
-   Coding conventions and important patterns.
-   Technical decisions and their rationale.
-   Verified limitations, known issues, and technical debt.

These are examples, not a mandatory checklist. Create a file only when
it has a distinct, useful responsibility. Combine small related topics
when that improves navigation. Split large topics when needed.

Do not duplicate complete source files, generated API documentation, or
information already maintained authoritatively elsewhere. Link to
authoritative project files where practical.

## 4. Strict Markdown line limit

Every Markdown file created or maintained inside the project memory
directory must contain at most 200 lines.

This is a hard maximum.

Before finishing any memory operation:

1.  Count the lines in every affected Markdown file.
2.  If a file exceeds 200 lines, split it into coherent topic files.
3.  Move content without losing meaning or important details.
4.  Update `index.md` and any affected internal links.
5.  Recount all affected files.
6.  Confirm that no memory Markdown file exceeds 200 lines.

Aim for approximately 100--170 lines where practical to leave room for
future updates. Do not pad short files or split them unnecessarily. Do
not evade the limit with dense, unreadable paragraphs.

The 200-line rule applies to memory Markdown files, including the index
and any project-memory linker documentation. It does not impose a limit
on application source files.

## 5. Maintain the memory index

`index.md` is the central navigation file.

It must:

-   List every memory file with a working relative link.
-   Explain the purpose and scope of each file.
-   Help Claude select relevant files for common task types.
-   Identify important relationships between topics when useful.
-   Avoid copying the contents of the linked files.

Update the index whenever a memory file is created, split, renamed,
moved, or deleted. Verify that every listed path exists and that every
memory file is represented.

## 6. Keep root CLAUDE.md as a lightweight linker

If root `CLAUDE.md` does not exist, create it. If it exists, preserve
unrelated project instructions and integrate the memory instructions
without replacing useful content.

Do not turn `CLAUDE.md` into the project encyclopedia. It must remain a
concise entry point.

It should contain only the essential memory workflow:

-   The location of the project memory.
-   A link to the memory index.
-   An instruction to read the index and relevant memory before broad
    source inspection.
-   An instruction to inspect relevant source code to verify memory.
-   An instruction to update affected memory after meaningful codebase
    changes.
-   An instruction to enforce the 200-line limit.

Do not duplicate architecture descriptions, module inventories,
dependency lists, or detailed project explanations in `CLAUDE.md`.

If the project already has a root `CLAUDE.md`, do not delete or rewrite
unrelated instructions. Keep the linker concise and compatible with the
existing file.

## 7. Incremental maintenance during project work

At the beginning of a project task:

1.  Read root `CLAUDE.md` and the memory index.
2.  Read only memory relevant to the requested task.
3.  Inspect relevant source code before relying on memory for
    implementation details.

After meaningful project changes, evaluate whether memory is affected.

Examples of changes that may require updates:

-   New, deleted, renamed, or reorganized modules.
-   Changed responsibilities, interfaces, or component relationships.
-   Architecture or data-flow changes.
-   Added, removed, or upgraded dependencies.
-   Changed build, test, packaging, or runtime procedures.
-   New established coding conventions.
-   Important technical decisions and their rationale.
-   Significant discovered limitations or resolved issues.

When memory is affected:

-   Update the smallest relevant set of memory files.
-   Preserve valid information and useful history.
-   Remove or revise facts that are now obsolete.
-   Update the index if navigation changes.
-   Recheck links and line counts.

Do not update memory for every trivial edit. A formatting-only change,
local typo fix, or temporary experiment normally does not need a
permanent memory entry unless it changes a documented convention or
reveals an important fact.

Do not claim that memory was updated unless the files were actually
changed and checked.

## 8. Accuracy and evidence

-   Document implemented behavior, not aspirations or unverified plans.
-   Distinguish current behavior, planned work, and unresolved
    questions.
-   Prefer concise explanations of responsibilities, relationships,
    interfaces, constraints, and workflows.
-   Record rationale for decisions when it is known and relevant.
-   Do not invent commands, paths, dependencies, capabilities, or
    architecture.
-   Do not store secrets, credentials, tokens, private keys, or
    sensitive user data.
-   Avoid copying large logs or transient debugging output into
    long-term memory.
-   When useful, identify the source files that substantiate important
    architectural claims.

If the repository contains conflicting documentation, identify the
conflict and verify against implementation before changing memory.

## 9. Validation checklist

Before completing initialization, refresh, audit, or maintenance,
verify:

-   [ ] The memory location follows the existing project convention, or
    defaults to `.claude/memory/`.
-   [ ] `index.md` exists and accurately lists the memory files.
-   [ ] Each memory file has a distinct, useful purpose.
-   [ ] Every memory Markdown file is at most 200 lines.
-   [ ] Internal memory links resolve to existing files.
-   [ ] Important claims are supported by inspected project evidence.
-   [ ] Stale or contradicted information has been corrected.
-   [ ] Root `CLAUDE.md` remains a concise linker, not a duplicate
    encyclopedia.
-   [ ] No unrelated application source files were changed during a
    memory-only task.

Do not mark a check as passed without verifying it.

## 10. Completion report

For initialization or audit, report:

-   Memory directory used.
-   Files created, updated, or split.
-   A short purpose statement for each memory file.
-   Line count for each memory Markdown file.
-   Important codebase areas inspected.
-   Known gaps or areas not verified.
-   Validation results.

For routine maintenance, provide a concise summary of the memory files
updated and why. If no memory update was necessary, say so and briefly
explain why.

## 11. Important automation boundary

A skill provides reusable instructions when Claude invokes it; it is not
itself a filesystem event listener.

The root `CLAUDE.md` linker is what carries the essential memory-first
and update-after-change workflow into ordinary future Claude Code
sessions.

Do not claim that this skill automatically detects edits made outside
Claude Code, by other tools, or in sessions where the relevant
instructions are not active.

If the user later requests deterministic event-driven updates, design
and configure an appropriate Claude Code hook as a separate, explicit
enhancement. Do not silently install hooks, modify settings, or
introduce scripts as part of ordinary memory initialization.

## Final principle

Keep project memory accurate, navigable, small, and continuously
relevant.

Read memory first. Verify against source. Inspect only what the task
needs. Update only what changed. Validate before finishing.
