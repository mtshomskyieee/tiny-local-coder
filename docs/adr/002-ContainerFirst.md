# ADR 002: Linux and apt supply the compilers

Status: Accepted

## Context

The agent compiles and runs the code it writes. Developer machines differ, and a Mac is not the Linux environment those commands assume. Installing compilers on the host, or through curl-pipe-sh and version managers, makes every session depend on whatever happened to be on PATH.

## Decision

Compile and run happen in the app container, `python:3.12-slim` ([Dockerfile](../../Dockerfile)). That base image ships the agent, not a compiler collection. Languages and their toolchains come from Debian **apt**, installed when a plan needs them and baked into the next image so the install is not repeated every session.

[`toolchains.py`](../../src/tinylocalcoder/toolchains.py) is the registry. Each language is one `Toolchain` entry: extension, legal run prefix, compile and smoke command, diagnostic regex, and `apt_packages`. Python, C, C++, Rust, Go, Ruby, Node, and Java are registered there. Other modules ask the registry; they do not hard-code a language.

A missing registered build tool is an environment failure. The execute path proposes `apt-get install` through the approval gate ([007](007-ApprovalGate.md)), appends the packages to `workspace/.toolchains`, and `start-service.sh` / `build-service.sh` pass that list as `EXTRA_APT_PACKAGES`.

A missing `./binary` is a build or plan failure. The recovery ladder treats it that way ([006](006-DeterministicStructure.md)).

## Consequences

- One Linux userspace is the contract, including on a Mac (Colima, Docker Desktop, or WSL2).
- Package versions are Debian's. Apt is the installer we accept; a newer rustc or Go from a version manager is out of scope.
- A runtime `apt-get` disappears when the container is recreated. What persists is `workspace/.toolchains` plus the next image build.
- Adding a language is one table entry and an integration runner ([004](004-GuardRails.md)).
