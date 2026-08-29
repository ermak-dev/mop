#!/bin/bash
# Slave-pool disk sweep (nomad job sl-cleanup, sysbatch+periodic).
#
# Three tiers, cheapest first, mirroring the classes in ~/bin/cleanup:
#
#   orphans     — a live slave == a live tmux session named after its job: the
#                 wrapper in ~/командлеты bin/ dies with its tmux session, and a
#                 stopped/moved job takes the session down with it. So any
#                 ~/wk/<name> or ~/.cache/target-<name> without an
#                 exactly-matching tmux session is an orphan: a deleted slave's
#                 leftovers (mop delete keeps them on purpose) or the trail of
#                 one that moved to another node. Cheap to re-create, so the rare
#                 race with a mop restarting at sweep time costs a re-clone.
#                 Swept unconditionally.
#   stale       — paths nothing references any more, age-gated. These are what a
#                 glob-driven sweep misses: $HOME/cache is rugent's pre-#627
#                 CARGO_TARGET_DIR, retired 2026-08-20, and matches neither
#                 ~/slaves/sl-* nor ~/.cache/target-sl-*. It still held 59 GB on
#                 gamer on 2026-08-28, four days after the last write.
#   size-capped — LIVE slaves' target dirs, trimmed by cargo-sweep, and only
#                 under space pressure. Not age-gated: cargo rewrites
#                 fingerprints on every build, so an active target dir never
#                 looks old (see the measurement in ~/bin/cleanup). Swept while
#                 holding cargo's own lock, so a build cannot be running in one
#                 while it is swept.
#
# WHICH FILESYSTEM IS ACTUALLY FULL
#
# On gamer the pool node is WSL2, and `df $HOME` is a lie: / is a 1007 GB sparse
# ext4.vhdx that reported 26% used / 714 GB free on 2026-08-28 while the Windows
# C: drive backing it was down to 8 GB and WSL would no longer boot
# (Wsl/Service/CreateInstance/E_FAIL) -- the node had been off the cluster for
# two days. The old sweep printed `df -h $HOME` and would have seen nothing
# wrong. So the gate is the TIGHTEST of $HOME and, on WSL, the backing store at
# /mnt/c.
#
# FREEING SPACE IN HERE DOES NOT, BY ITSELF, GIVE C: A BYTE BACK.
#
# / is mounted `discard` and `fstrim --dry-run` reports 0 B pending, which looks
# like the blocks are being returned. They are not: measured on 2026-08-28,
# deleting 102 GB inside WSL took / from 242 GB used to 140 GB while
# ext4.vhdx stayed at 255.13 GB and C: free did not move (123.0 -> 122.5 GB).
# The vhdx carries the plain `Archive` attribute, not `SparseFile`, so the host
# file only ever grows to its high-water mark and discards inside the guest
# deallocate nothing on NTFS.
#
# Returning the space needs the vhdx made sparse -- `wsl --manage <d>
# --set-sparse true` for the existing one, `[experimental] sparseVhd=true` in
# .wslconfig for any new one -- both of which need the distro stopped. Until
# that is done, this sweep buys room inside the guest (which is what stops a
# slave's build failing) but the guest's footprint on C: only ratchets up, and
# C: is what actually stopped this node.
#
# Env knobs (set in the nomad job spec):
#   MOP_SWEEP_FREE_MIN_GB escalate to tier 3 below this much free   (default 60)
#   MOP_SWEEP_MAX_TARGET  per-target-dir cap for cargo-sweep      (default 15GB)
#   MOP_SWEEP_STALE_DAYS   age gate for tier 2                      (default 14)
#   MOP_SWEEP_DRY          set to 1 to report without deleting
set -u

FREE_MIN_GB=${MOP_SWEEP_FREE_MIN_GB:-${WK_SWEEP_FREE_MIN_GB:-60}}
MAX_TARGET=${MOP_SWEEP_MAX_TARGET:-${WK_SWEEP_MAX_TARGET:-15GB}}
STALE_DAYS=${MOP_SWEEP_STALE_DAYS:-${WK_SWEEP_STALE_DAYS:-14}}
DRY=${MOP_SWEEP_DRY:-}

# cargo and cargo-sweep live in ~/.cargo/bin, added by the login shell's rc
# file. raw_exec does not source that, so this task sees a PATH without them.
PATH="$HOME/.cargo/bin:$PATH"

MIN_KB=1024   # ignore anything under 1 MB -- not worth the report line

hr() {
    awk -v kb="$1" 'BEGIN{
        if (kb >= 1048576) printf "%.1f GB", kb/1048576;
        else if (kb >= 1024) printf "%.1f MB", kb/1024;
        else printf "%d KB", kb;
    }'
}

note() { printf '  %-46s %10s\n' "$1" "$2"; }

freed_kb=0

# A live slave == a live session on ITS OWN tmux server: each slave runs
# `tmux -L <job>` since the shared-server cgroup OOM incident.
live_player() { tmux -L "$1" has-session -t "=$1" 2>/dev/null; }

# Free GB on the filesystem holding $1. Both ext4 and the 9p /mnt/c answer this.
free_gb() { df -BG --output=avail "$1" 2>/dev/null | awk 'NR==2{print $1+0}'; }

# The filesystems whose exhaustion actually stops this node. On WSL the vhdx
# backing store is the real limit and is invisible from `df $HOME`.
WATCH=("$HOME")
if [ -d /mnt/c ] && grep -qi microsoft /proc/version 2>/dev/null; then
    WATCH+=(/mnt/c)
fi

# The tightest of them, in GB.
pressure_gb() {
    local min="" g
    for p in "${WATCH[@]}"; do
        g=$(free_gb "$p") || continue
        [ -z "$g" ] && continue
        { [ -z "$min" ] || [ "$g" -lt "$min" ]; } && min="$g"
    done
    echo "${min:-999999}"
}

report_space() {
    for p in "${WATCH[@]}"; do
        printf '  %-14s %s\n' "$p" "$(df -h "$p" 2>/dev/null | awk 'NR==2{printf "%s used of %s, %s free (%s)", $3, $2, $4, $5}')"
    done
}

# rm_path <path> <label> -- remove and account for a whole path.
rm_path() {
    local path="$1" label="$2" kb
    [ -e "$path" ] || return 0
    kb=$(du -sk "$path" 2>/dev/null | awk '{print $1}')
    [ -z "$kb" ] && return 0
    [ "$kb" -lt "$MIN_KB" ] 2>/dev/null && return 0
    note "$label" "$(hr "$kb")"
    freed_kb=$((freed_kb + kb))
    [ -n "$DRY" ] && return 0
    # Slave clones are ermak's, but anything a container wrote into a cache is
    # root's; ermak cannot unlink those. `sudo -n` never prompts, and where it
    # is not permitted this fails exactly as it did before.
    rm -rf "$path" 2>/dev/null || sudo -n rm -rf "$path" 2>/dev/null
}

# Does $1 hold anything written in the last $2 days? A directory's own mtime
# only moves when an entry is added or removed directly in it, so a tree that is
# still being written can carry a weeks-old timestamp. -quit stops at the first
# hit, so this is one stat in the common case, not a walk of the whole tree.
has_recent() {
    [ -n "$(find "$1" -mtime -"$2" -print -quit 2>/dev/null)" ]
}

echo "=== disk before ==="
report_space
[ -n "$DRY" ] && echo "*** DRY RUN -- nothing will be deleted ***"
echo

# --------------------------------------------------------------------------
# Tier 1 -- orphaned slave clones and target dirs. Always.
# --------------------------------------------------------------------------
# Two sanity gates before anything is deleted. Both exist because "no live tmux
# session" is only evidence of an orphan when tmux could have answered at all --
# and on 2026-08-28 mirror spent nine minutes in a state where it could not:
# the box was hard-reset, /home/ermak is ecryptfs and comes back UNMOUNTED, and
# the slaves only start once someone logs in with a password. A sweep landing
# in that window would have seen every clone with no session and deleted the lot.
# Nightly, that window was a rounding error; hourly it is a real exposure.
#
# Gate 1: an unmounted ecryptfs home is not an empty home. It presents a
# placeholder (Access-Your-Private-Data.desktop / README.txt) instead of the
# real tree, so ~/slaves is simply absent and every glob below silently matches
# nothing. Harmless today, but bail loudly rather than report a clean sweep of
# a filesystem we never actually looked at.
# Два корня клонов и два корня target-ов. Второй в каждой паре -- наследство
# переименования slave -> slave (2026-08-29): в ~/wk остались клоны прежнего
# пула, часть из них с НЕсохранённой работой, и сторож обязан продолжать их
# видеть. Убрать legacy-глоб можно будет, когда ~/wk опустеет.
CLONE_GLOBS=("$HOME"/slaves/sl-* "$HOME"/wk/wk-*)
TARGET_GLOBS=("$HOME"/.cache/target-sl-* "$HOME"/.cache/target-wk-*)

if [ -e "$HOME/Access-Your-Private-Data.desktop" ] \
    || { [ ! -d "$HOME/slaves" ] && [ ! -d "$HOME/wk" ]; }; then
    echo "  ! \$HOME has no slaves/ (unmounted ecryptfs?) -- refusing to sweep" >&2
    report_space
    exit 0
fi

# Gate 2: clones exist but NOT ONE has a live tmux server. On a node that hosts
# slaves that is not a pile of orphans, it is tmux being unreachable -- the node
# just booted, or this task cannot see /tmp/tmux-$(id -u). Deleting every clone
# on the node is never the right answer to that, and a genuine all-orphans node
# is rare enough to do by hand.
clones=0; live=0
for d in "${CLONE_GLOBS[@]}"; do
    [ -d "$d" ] || continue
    clones=$((clones + 1))
    live_player "$(basename "$d")" && live=$((live + 1))
done
if [ "$clones" -gt 0 ] && [ "$live" -eq 0 ]; then
    echo "  ! $clones clone(s), 0 live tmux servers -- tmux unreachable, not $clones orphans" >&2
    echo "  ! refusing tier 1; tiers 2-3 still run" >&2
    SKIP_TIER1=1
fi

echo "tier 1: orphaned slave dirs ($live/$clones slaves live)"
[ -n "${SKIP_TIER1:-}" ] && echo "  skipped (see warning above)"
for d in "${CLONE_GLOBS[@]}"; do
    [ -n "${SKIP_TIER1:-}" ] && break
    [ -d "$d" ] || continue
    n=$(basename "$d")
    live_player "$n" && continue
    rm_path "$d" "orphaned clone $n"
done
for t in "${TARGET_GLOBS[@]}"; do
    [ -n "${SKIP_TIER1:-}" ] && break
    [ -d "$t" ] || continue
    n=${t##*/target-}
    live_player "$n" && continue
    rm_path "$t" "orphaned target $n"
done

# --------------------------------------------------------------------------
# Tier 2 -- retired paths nothing references, age-gated. Always.
# --------------------------------------------------------------------------
echo "tier 2: retired paths idle > ${STALE_DAYS}d"
# $HOME/cache: rugent's CARGO_TARGET_DIR before #627 moved it to
# ~/.cache/target-<slave>. Left behind on every node that built there.
for stale in "$HOME/cache"; do
    [ -d "$stale" ] || continue
    if has_recent "$stale" "$STALE_DAYS"; then
        note "$(basename "$stale") (written recently, kept)" "-"
        continue
    fi
    rm_path "$stale" "retired path $stale"
done

# --------------------------------------------------------------------------
# Tier 3 -- size-cap LIVE slaves' target dirs, only under pressure.
# --------------------------------------------------------------------------
avail=$(pressure_gb)
if [ "$avail" -ge "$FREE_MIN_GB" ]; then
    echo "tier 3: skipped -- ${avail} GB free on the tightest watched fs (>= ${FREE_MIN_GB} GB)"
else
    echo "tier 3: ${avail} GB free < ${FREE_MIN_GB} GB -- capping live target dirs at ${MAX_TARGET}"
    live_targets=()
    for t in "${TARGET_GLOBS[@]}"; do
        [ -d "$t" ] || continue
        live_targets+=("$t")
    done
    if [ ${#live_targets[@]} -eq 0 ]; then
        echo "  no live target dirs"
    else
        # incremental/ needs no tooling, so it is dropped even where cargo-sweep
        # is missing. Only the size cap needs the binary, and there is
        # deliberately no mtime fallback for it: on a target dir that is still
        # being built into an age gate matches nothing (0 of 34,509 files older
        # than 3 days in a 48 GB dir) while a size cap finds tens of GB, so a
        # fallback would report success and free nothing.
        have_sweep=true
        if ! command -v cargo-sweep >/dev/null 2>&1; then
            have_sweep=false
            echo "  ! cargo-sweep missing -- capping skipped, incremental/ still dropped" >&2
            echo "  ! install with: cargo install cargo-sweep --locked" >&2
        fi
        # cargo holds an exclusive flock on target/<profile>/.cargo-lock for as
        # long as it builds. Taking the same locks non-blocking is exact:
        # holding them means no build is in that directory and none can start
        # until we let go. A job that arrives mid-sweep waits ("Blocking waiting
        # for file lock on build directory") instead of losing an .rlib out from
        # under itself.
        swargs="--maxsize $MAX_TARGET"
        [ -n "$DRY" ] && swargs="--dry-run $swargs"

        for t in "${live_targets[@]}"; do
            locks=""
            for l in "$t"/*/.cargo-lock "$t"/*/.cargo-build-lock "$t"/*/.cargo-artifact-lock; do
                [ -f "$l" ] && locks="$locks $l"
            done
            before=$(du -sk "$t" 2>/dev/null | awk '{print $1}')
            inc_before=$freed_kb
            if [ -n "$locks" ]; then
                # The watchdog matters: if this task is killed rather than
                # exiting, a stray holder would block every build on the host.
                # Once the shell that spawned it is gone the parent becomes init
                # and it lets go.
                fifo=$(mktemp -u) && mkfifo "$fifo" || continue
                python3 -c '
import fcntl, os, sys, time
held = []
for path in sys.argv[1:]:
    f = open(path)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(1)
    held.append(f)
print("locked", flush=True)
while os.getppid() != 1:
    time.sleep(1)
' $locks > "$fifo" 2>/dev/null &
                lock_pid=$!
                read -r ok < "$fifo"; rm -f "$fifo"
                if [ "${ok:-}" != locked ]; then
                    note "$(basename "$t") (build running, skipped)" "-"
                    continue
                fi
            else
                lock_pid=""
            fi
            # incremental/ first: cargo-sweep weighs only the artifacts
            # `cargo metadata` knows about, so it walks straight past this one
            # -- on mirror it held 62 GB that a 304 GiB sweep left sitting
            # there, and on gamer 46 GB across three live slaves on
            # 2026-08-28. Regenerable at any age: every run builds a different
            # commit and cargo never reuses a byte of it.
            for inc in "$t"/*/incremental; do
                [ -d "$inc" ] || continue
                rm_path "$inc" "$(basename "$t")/$(basename "$(dirname "$inc")")/incremental"
            done

            # cargo-sweep asks `cargo metadata` where the artifacts are rather
            # than guessing path shapes -- the guessing is what hid 106 GB on
            # win from ~/bin/cleanup for months. No network at sweep time;
            # lockfiles are committed.
            if $have_sweep; then
            out=$(CARGO_NET_OFFLINE=true cargo-sweep sweep -r $swargs "$t" 2>&1)
            # Note cargo-sweep's asymmetric wording: "Would clean: 34 GiB from"
            # in dry run, "Cleaned 34 GiB from" (no colon) when it deletes.
            # Matching only the dry-run form once reported "0 KB" while it was
            # freeing 33.7 GB.
            printf '%s\n' "$out" \
              | sed -n 's/^\[INFO\] \(Would clean: \|Cleaned \)\(.*\) from "\(.*\)"$/\3|\2/p' \
              | while IFS='|' read -r path amount; do
                    # "Cleaned nothing from X" means it was already under the
                    # cap -- not a line worth a row in the report.
                    [ "$amount" = nothing ] && continue
                    note "swept $(basename "$path")" "$amount"
                done
            printf '%s\n' "$out" | grep -vE '^\[INFO\]|^$' | sed 's/^/  ! /' >&2
            fi

            [ -n "$lock_pid" ] && kill "$lock_pid" 2>/dev/null
            inc_kb=$((freed_kb - inc_before))
            # Trust the measured delta over the parsed report: rm_path already
            # counted incremental/, and cargo-sweep's own figure is binary-unit
            # text. Recount from du and drop what rm_path added for this dir.
            after=$(du -sk "$t" 2>/dev/null | awk '{print $1}')
            if [ -n "$before" ] && [ -n "$after" ] && [ "$before" -gt "$after" ]; then
                freed_kb=$((freed_kb - inc_kb + before - after))
            fi
        done
    fi
fi

echo
echo "freed $(hr "$freed_kb")"
echo "=== disk after ==="
report_space

# Leave a machine-readable line for `nomad alloc logs` and any future scrape.
echo "wk_sweep_freed_kb=$freed_kb wk_sweep_free_gb=$(pressure_gb)"
