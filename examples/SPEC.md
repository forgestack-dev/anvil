# Repository status line

A small command that prints one line summarising a Git checkout, for use in a
shell prompt. Copy this file into the repository you want Anvil to work on,
commit it there, and run `anvil prepare` against it: a specification must be
committed inside the target repository.

It is written to the [criterion contract](../docs/CRITERIA.md). Every
requirement below states how it is decided, and the headings are the lines a
prepared ticket cites in `source_refs`.

## Scope

### R1: Print one status line

`statusline` prints exactly one line to stdout and exits 0 in a Git working
tree. The line is the branch name, a space, and the short commit ID.

Decided by the test suite: a test creates a temporary repository, runs the
command, and asserts the exact output for a known branch and commit.

### R2: Report a detached HEAD without failing

In a detached HEAD the line reads `detached` in place of the branch name, and
the exit status is still 0.

Decided by the test suite, as a second case of the R1 test.

### R3: Fail cleanly outside a repository

Outside a Git working tree the command prints a single diagnostic line to
stderr, prints nothing to stdout, and exits 1.

Decided by the test suite: a test runs the command in an empty temporary
directory and asserts the streams and the exit status separately.

### R4: Start no subprocess other than Git

The command's only child process is `git`. This absence holds over the complete
set of process-starting calls in the new module: `subprocess.run`,
`subprocess.Popen`, `os.system`, `os.exec*`, and `os.spawn*`.

Decided by inspection of the new module's diff against that enumerated list. It
is not "no subprocesses anywhere", which nothing could finish against.

## Out of scope

Colour output, shell-specific escaping, dirty-tree markers, upstream tracking
counts, and any caching of Git results. A ticket proposing one of these is out
of scope for this specification rather than an omission from it.
