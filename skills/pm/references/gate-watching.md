# Watching the full gate

Read this before your first landing of the session. Watching a gate wrongly
went wrong three times in one day, in three costumes, and the shape is always
the same: **what is watched is not the run.**

## Before starting a run

- **A killed wrapper does NOT kill the run underneath it.** One gate outlived
  its wrapper, a second was started, and two suites ground against one build
  directory, manufacturing failures nobody could reproduce. Check that no run
  is already going before starting one.
- **Start the run detached and redirected to a file.** A plain background job
  dies with the session's other tasks — that killed one red run at its halfway
  point. Piping through `tail` buffers until EOF, hiding progress and exit
  status alike.
- **Suite and lint in sequence, suite first.** Started together they queue
  behind the build tool's lock, and a gate waiting on a lock is
  indistinguishable from one that has hung.

## The watcher

- **Remembering one pid is not enough.** A launcher may spawn a wrapper and
  the real run beneath it; the wrapper exits early, the pid dies, the log sits
  mid-compile — and the watcher is one report away from crying red over a gate
  that is still running.
- So watch for the **absence of any process of the run**, not the death of a
  chosen pid, AND require the **anchored summary line** in the log. Neither
  alone suffices: the pid lies about the wrapper, the summary knows nothing
  about the teardown that follows it. Anchor the pattern — a bare `error:` or
  `FAILED` matches the NAME of a passing test.
- **Give the third outcome its own exit.** Process gone with no summary means
  the run was KILLED — neither green nor red. Say so loudly and fail the
  watch, or the silence gets read as success from the other side.

## The exit code you are shown is not the gate's

The gate's status must come from INSIDE the run, written into its own log:

```
{ ./bin/clo fmt; echo "===EXIT=$?==="; } > gate.log 2>&1
```

Every other route to it lies in a way that reads as success:

- **A pipe reports the last stage.** `clo test … | tail` gives you `tail`'s
  status — 0 over a 404, 0 over a failed suite. Never pipe anything whose exit
  status you intend to read.
- **A background task's reported status is the WRAPPER's**, not the command's,
  whenever anything is chained after it — a trailing `echo` is enough. An agent
  read that 0 and was one message away from reporting "the gate exits 0 despite
  errors" as a tooling defect. The gate had exited 1, correctly.
- Both failures point the same way: the log is the honest signal, and an exit
  code recovered from outside the run is a reconstruction. Make the run record
  its own verdict and read that.

**A gate that stops early can still print a green tail.** Where the format step
runs phases under `set -e`, a failure in the first aborts the rest — so "no
errors" from a run that never reached the later phases is a statement about a
subset. Confirm each phase was REACHED by its own marker in the log, not by the
absence of complaints.

**A gate blocked on a machine-wide lock is not a hung gate.** Where suites
serialise on one lock, a neighbour's run holds it and yours waits — correct
behaviour that looks identical to a hang. Identify the holder before concluding
anything, and confirm any lock you observe held is your own run's before
claiming a measurement about it.
