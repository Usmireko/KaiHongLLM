# Refactor Report

Date: 2026-06-24

## Summary

The workspace was reorganized into `data_train_test/` and
`closed_loop_demo/`, with Board/Host/Server boundaries inside each line. Core
entrypoints were moved into canonical subproject paths.

## Cleanup Follow-Up

The follow-up cleanup removed legacy root compatibility wrappers and generated
workspace history after backing candidates up to an external sibling directory.
Use `scripts/*.sh` or the canonical paths under `data_train_test/` and
`closed_loop_demo/`.

## Validation Scope

Validation was limited to static checks in this environment. Hardware HDC,
server SSH, model loading, and real demo execution still require the matching
board/server setup.
