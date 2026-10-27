#!/bin/bash
# Local ledger history. Restore: rsync -a "$DST/snap/<snapshot or current>/" ~/.do-it/
# Legacy-only files can also be copied from "$DST/rescued/".
# Parse the entire body before executing it, so an in-place deploy is harmless.
main() {
    local SRC="${SNAPSHOT_SRC:-/home/albert/.do-it}"
    local DST="${SNAPSHOT_DST:-/var/backups/do-it}"
    if [[ ! -d "$SRC/events" ]]; then
        echo 'snapshot: source events/ missing' >&2
        return 7
    fi
    umask 077
    export LC_ALL=C TZ=UTC
    set -o pipefail
    local now day hour test_hooks=0 first_error='' status=0 lockfd
    if [[ ${SNAPSHOT_TEST:-} == 1 && $(readlink -m -- "$DST") != /var/backups/do-it ]]; then
        test_hooks=1
    fi
    now=$(date -u +%s)
    if (( test_hooks )) && [[ -n ${SNAPSHOT_NOW:-} ]]; then
        now=$(date -u -d "$SNAPSHOT_NOW" +%s) || { echo 'snapshot: clock failed rc=1' >&2; return 4; }
    fi
    day=$(date -u -d "@$now" +%F)
    hour=$(date -u -d "@$now" +%FT%H)
    mkdir -p -- "$DST" || { echo 'snapshot: mkdir failed rc=1' >&2; return 4; }
    exec {lockfd}>"$DST/snapshot.lock" || { echo 'snapshot: lock failed rc=1' >&2; return 4; }

    # /proc comm may contain both spaces and ')': count fields after its LAST ')'.
    proc_start() {
        local line fields
        [[ -r /proc/$1/stat ]] || return 1
        IFS= read -r line < "/proc/$1/stat" || return 1
        read -r -a fields <<< "${line##*) }"
        printf '%s' "${fields[19]}"
    }
    if ! flock -n "$lockfd"; then
        local pid birth stamp started threshold=3600 actual original replacement
        [[ -e "$DST/snap/.current-building" || ( -e "$DST/.cutover-renamed" && ! -d "$DST/snap/current" ) ]] && threshold=14400
        if [[ -r "$DST/snapshot.running" ]]; then
            original=$(<"$DST/snapshot.running")
            read -r pid birth stamp <<< "$original"
            if [[ $pid =~ ^[0-9]+$ && $birth =~ ^[0-9]+$ ]] && started=$(date -u -d "$stamp" +%s 2>/dev/null) && (( now - started > threshold )); then
                actual=$(proc_start "$pid")
                if [[ $actual == "$birth" ]]; then
                    echo "snapshot: previous run stuck since $stamp (pid $pid)" >&2
                    return 6
                fi
                sleep 2
                replacement=$(cat "$DST/snapshot.running" 2>/dev/null)
                actual=$(proc_start "$pid")
                if [[ $original == "$replacement" && $actual != "$birth" ]]; then
                    echo "snapshot: lock orphaned since $stamp (pid $pid gone)" >&2
                    return 6
                fi
            fi
        fi
        return 0
    fi

    fail() {
        local message="snapshot: $1 failed rc=$2"
        printf '%s\n' "$message" >> "$DST/snapshot.log"
        [[ -n $first_error ]] || first_error=$message
        status=4
    }
    step() {
        local label=$1 rc
        shift
        "$@" >> "$DST/snapshot.log" 2>&1
        rc=$?
        (( rc == 0 )) || fail "$label" "$rc"
        return "$rc"
    }
    note() { printf '%s\n' "$*" >> "$DST/snapshot.log"; }
    # Count before every recursive removal; the log exposes both count and reason.
    remove_tree() {
        local count
        count=$(find "$2" -printf . 2>>"$DST/snapshot.log" | wc -c) || { fail "$1-count" "$?"; return 1; }
        note "remove reason=$1 entries=$count path=$2"
        step "$1" rm -rf -- "$2"
    }
    rotate_log() {
        if [[ -f "$DST/snapshot.log" ]] && (( $(stat -c %s "$DST/snapshot.log") > 10*1024*1024 )); then
            tail -c 1048576 "$DST/snapshot.log" > "$DST/snapshot.log.tmp" || { fail log-tail "$?"; return 1; }
            step log-rename mv -- "$DST/snapshot.log.tmp" "$DST/snapshot.log" || return 1
        fi
        return 0
    }
    finish() {
        rotate_log
        trap - EXIT
        step cleanup rm -f -- "$DST/snapshot.running" "$DST/.rescue-list" "$DST/.tar-list"
        if [[ -n $first_error ]]; then printf '%s\n' "$first_error" >&"$errfd"; fi
        return "$status"
    }
    cleanup() {
        local DST=$1
        rm -f -- "$DST/snapshot.running" "$DST/.rescue-list" "$DST/.tar-list" >> "$DST/snapshot.log" 2>&1
    }
    trap "cleanup $(printf '%q' "$DST")" EXIT
    local start errfd
    exec {errfd}>&2
    exec 2>>"$DST/snapshot.log"
    start=$(proc_start "$$")
    printf '%s %s %s\n' "$$" "$start" "$(date -u -d "@$now" +%FT%TZ)" > "$DST/snapshot.running.tmp"
    step running-rename mv -- "$DST/snapshot.running.tmp" "$DST/snapshot.running" || { finish; return $?; }
    rotate_log || { finish; return $?; }
    if (( test_hooks )); then
        while [[ -n ${SNAPSHOT_TEST_PAUSE_FILE:-} && -e $SNAPSHOT_TEST_PAUSE_FILE ]]; do sleep .05; done
    fi
    step mkdir mkdir -p -- "$DST/snap" || { finish; return $?; }
    shopt -s nullglob

    old_running() {
        local path args
        for path in /proc/[0-9]*/cmdline; do
            [[ -r $path ]] || continue
            args=()
            mapfile -d '' -t args < "$path" 2>/dev/null || continue
            [[ ${args[0]:-} == "$DST/snapshot.sh" ]] && return 0
            if [[ ${args[0]##*/} == bash || ${args[0]##*/} == sh ]]; then
                [[ ${args[1]:-} == "$DST/snapshot.sh" ]] && return 0
            fi
        done
        return 1
    }
    # One walk per rescue; no allow-list of valuable paths can miss an unknown file.
    # ln -P and stat without -L preserve a symlink itself, including dangling links.
    rescue() {
        local tree=$1 target=$2 verify=${3:-0} path rel dest inode other size
        local count=0 bytes=0 missed=0 failed=0
        # Keep command output separate from the log: this list is NUL-delimited.
        find "$tree" \( -type d \( -path "$tree/build-tmp" -o -path "$tree/tmp-*" -o -path "$tree/content/probe-*" \) -prune \) -o \( \( -type f -o -type l \) -print0 \) > "$DST/.rescue-list" 2>>"$DST/snapshot.log" || { fail rescue "$?"; return 1; }
        while IFS= read -r -d '' path; do
            rel=${path#"$tree/"}
            [[ ${rel##*/} =~ ^\..+\.[A-Za-z0-9]{6}$ ]] && continue
            [[ -e "$SRC/$rel" || -L "$SRC/$rel" ]] && continue
            dest=$target/$rel
            inode=$(stat -c '%d:%i' -- "$path") || { fail rescue "$?"; failed=1; continue; }
            size=$(stat -c %s -- "$path") || { fail rescue "$?"; failed=1; continue; }
            (( count+=1, bytes+=size ))
            if [[ -e $dest || -L $dest ]]; then
                other=$(stat -c '%d:%i' -- "$dest")
                [[ $other == "$inode" ]] && continue
                fail rescue 1
                failed=1
                continue
            fi
            missed=1
            step rescue mkdir -p -- "${dest%/*}" && step rescue ln -P -- "$path" "$dest" || failed=1
        done < "$DST/.rescue-list"
        note "rescue files=$count bytes=$bytes directory=$target repaired=$missed"
        (( failed == 0 )) || return 1
        (( verify == 0 || missed == 0 ))
    }
    tarballs() {
        local archive path rel missing
        for archive in "$DST"/daily-*.tar.gz.part; do remove_tree tarball-part "$archive"; done
        for archive in "$DST"/daily-*.tar.gz; do
            nice -n 19 tar -tzf "$archive" > "$DST/.tar-list" 2>>"$DST/snapshot.log"
            local rc=$?
            if (( rc != 0 )); then fail tarball-list "$rc"; continue; fi
            missing=0
            while IFS= read -r path; do
                [[ $path == */ ]] && continue
                rel=${path#./}; rel=${rel#mirror/}
                case "$rel" in build-tmp/*|tmp-*/*|content/probe-*/*) continue;; esac
                [[ -e "$DST/mirror.legacy/$rel" || -L "$DST/mirror.legacy/$rel" ]] || (( missing+=1 ))
            done < "$DST/.tar-list"
            if (( missing == 0 )); then remove_tree tarball "$archive";
            else note "tarball kept missing=$missing path=$archive"; fi
        done
    }
    local cutover_allowed=0 rename_day late late_name
    # Scan only for existing legacy state; anything arriving later waits a run.
    if [[ -e "$DST/mirror" || -e "$DST/mirror.legacy" || -e "$DST/.cutover-renamed" ]]; then
        cutover_allowed=1
        old_running && cutover_allowed=0
    fi
    if (( cutover_allowed )); then
        if [[ ! -f "$DST/.cutover-renamed" && -d "$DST/mirror" ]]; then
            if step cutover-rename mv -- "$DST/mirror" "$DST/mirror.legacy"; then
                printf '%s\n' "$day" > "$DST/.cutover-renamed"
            fi
        fi
        # Recover a crash immediately after mv but before the marker write.
        if [[ -d "$DST/mirror.legacy" && ! -f "$DST/.cutover-renamed" ]]; then
            date -u -r "$DST/mirror.legacy" +%F > "$DST/.cutover-renamed"
        fi
        if [[ -f "$DST/.cutover-renamed" ]]; then
            rename_day=$(<"$DST/.cutover-renamed")
            tarballs
            if [[ ! -f "$DST/.rescue-done" && -d "$DST/mirror.legacy" ]]; then
                if rescue "$DST/mirror.legacy" "$DST/rescued/legacy-$rename_day"; then
                    printf '%s\n' "$day" > "$DST/.rescue-done"
                fi
            fi
            if [[ -d "$DST/mirror" ]]; then
                late_name=$(date -u -d "@$now" +%Y-%m-%dT%H%M%S)-$$
                step late-rename mv -- "$DST/mirror" "$DST/mirror.legacy.late-$late_name"
            fi
            for late in "$DST"/mirror.legacy.late-*; do
                [[ -d $late ]] || continue
                late_name=${late##*/mirror.legacy.late-}
                if rescue "$late" "$DST/rescued/legacy-late-$late_name"; then
                    remove_tree late-removal "$late"
                    if [[ -f "$DST/.rescue-done" ]]; then
                        printf '%s\n' "$day" > "$DST/.rescue-done"
                    fi
                fi
            done
        fi
    fi

    # Collect only validated, dated directories. Future clocks cannot consume history.
    local -a snapshots=() epochs=()
    collect() {
        local path name epoch
        snapshots=(); epochs=()
        for path in "$DST"/snap/*; do
            name=${path##*/}
            [[ -d $path && ! -L $path && $name =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}$ ]] || continue
            epoch=$(date -u -d "${name/T/ }:00:00" +%s 2>/dev/null) || continue
            if (( epoch > now + 3600 )); then note "future snapshot ignored path=$path"; continue; fi
            snapshots+=("$name"); epochs+=("$epoch")
        done
    }
    retention() {
        collect
        local i name date_key date_epoch today newest='' second=''
        local -A daily=()
        today=$(date -u -d "$day" +%s)
        for i in "${!snapshots[@]}"; do
            if (( epochs[i] <= now )); then second=$newest; newest=${snapshots[i]}; fi
        done
        for i in "${!snapshots[@]}"; do
            name=${snapshots[i]}; date_key=${name%T*}
            date_epoch=$(date -u -d "$date_key" +%s)
            if [[ -z ${daily[$date_key]:-} ]]; then
                daily[$date_key]=$name
                if (( date_epoch >= today - 7*86400 && date_epoch <= today )); then continue; fi
            fi
            if (( epochs[i] >= now - 86400 )) || [[ $name == "$newest" || $name == "$second" ]]; then continue; fi
            remove_tree retention "$DST/snap/$name"
        done
    }
    local free size held stats rc sync_ok=1 building=0
    free=$(df -B1 --output=avail "$DST" | tail -1)
    if (( test_hooks )) && [[ -n ${SNAPSHOT_TEST_FREE_BYTES:-} ]]; then free=$SNAPSHOT_TEST_FREE_BYTES; fi
    local -a filters=(--filter 'P /burndown/'
        --exclude .git --exclude worktrees --exclude repos --exclude '_fixtures-*'
        --exclude scratch --exclude tmp-scratch --exclude release-runner
        --exclude portal-e2e-runner --exclude seat --exclude 'scratch-*'
        --exclude /burndown --exclude __pycache__ --exclude /cache
        --exclude 'burndown/*/wt' --exclude 'burndown/*/wt-*' --exclude /tmp
        --exclude /build-tmp/ --exclude '/tmp-*/')
    if (( free < 2*1024*1024*1024 )); then
        [[ -n $first_error ]] || first_error="snapshot: low space ($free bytes free)"
        note "snapshot: low space ($free bytes free)"; status=4; sync_ok=0
    elif [[ ! -d "$DST/snap/current" || -e "$DST/snap/.current-building" ]]; then
        building=1
        # --stats measures selected bytes, not all scratch under SRC.
        stats=$(rsync -an --stats "${filters[@]}" "$SRC/" "$DST/snap/current/" 2>>"$DST/snapshot.log")
        rc=$?
        note "$stats"
        if (( rc == 23 )); then fail rsync "$rc";
        elif (( rc != 0 && rc != 24 )); then fail rsync "$rc"; sync_ok=0; fi
        size=$(sed -n 's/^Total file size: \([0-9,]*\) bytes.*/\1/p' <<< "$stats")
        size=${size//,/}
        if [[ ! $size =~ ^[0-9]+$ ]]; then fail source-size 1; size=0; sync_ok=0; fi
        if [[ -d "$SRC/burndown" ]]; then
            stats=$(rsync -an --stats --prune-empty-dirs --exclude 'wt/' --exclude 'wt-*/' --include '*/' --include '*.md' --exclude '*' "$SRC/burndown/" "$DST/snap/current/burndown/" 2>>"$DST/snapshot.log")
            rc=$?
            note "$stats"
            if (( rc == 23 )); then fail rsync "$rc";
            elif (( rc != 0 && rc != 24 )); then fail rsync "$rc"; sync_ok=0; fi
            local markdown_size
            markdown_size=$(sed -n 's/^Total file size: \([0-9,]*\) bytes.*/\1/p' <<< "$stats")
            markdown_size=${markdown_size//,/}
            if [[ $markdown_size =~ ^[0-9]+$ ]]; then (( size+=markdown_size ));
            else fail source-size 1; sync_ok=0; fi
        fi
        held=0
        [[ ! -d "$DST/snap/current" ]] || read -r held _ < <(du -sb "$DST/snap/current")
        if (( free < size - held + 5*1024*1024*1024 )); then
            [[ -n $first_error ]] || first_error='snapshot: insufficient space to build current'
            note 'snapshot: insufficient space to build current'; status=4; sync_ok=0
        fi
    fi
    sync_pass() {
        rsync "$@" >> "$DST/snapshot.log" 2>&1
        local rc=$?
        if (( rc == 23 )); then fail rsync "$rc"; return 0; fi
        if (( rc != 0 && rc != 24 )); then fail rsync "$rc"; return 1; fi
    }
    if (( sync_ok )); then
        if (( building )); then : > "$DST/snap/.current-building"; fi
        sync_pass -a --delete --delete-excluded "${filters[@]}" "$SRC/" "$DST/snap/current/" || sync_ok=0
        if (( sync_ok )); then
            if [[ -d "$SRC/burndown" ]]; then
                sync_pass -a --delete --delete-excluded --prune-empty-dirs --exclude 'wt/' --exclude 'wt-*/' --include '*/' --include '*.md' --exclude '*' "$SRC/burndown/" "$DST/snap/current/burndown/" || sync_ok=0
            elif [[ -d "$DST/snap/current/burndown" ]]; then
                remove_tree burndown-removal "$DST/snap/current/burndown" || sync_ok=0
            fi
        fi
        if (( sync_ok )); then
            step build-marker rm -f -- "$DST/snap/.current-building" || sync_ok=0
        fi
    fi
    if (( sync_ok )); then
        local part
        for part in "$DST"/snap/*.part; do remove_tree interrupted-freeze "$part" || sync_ok=0; done
        if (( sync_ok )) && [[ ! -d "$DST/snap/$hour" ]]; then
            if step cp cp -al -- "$DST/snap/current" "$DST/snap/$hour.part" && step freeze-rename mv -- "$DST/snap/$hour.part" "$DST/snap/$hour"; then
                step latest ln -sfn -- "$hour" "$DST/snap/latest"
            fi
        fi
    fi
    retention
    if (( cutover_allowed )) && [[ -d "$DST/mirror.legacy" && -f "$DST/.rescue-done" ]]; then
        local -A dates=()
        local name
        collect
        for name in "${snapshots[@]}"; do dates[${name%T*}]=1; done
        if (( ${#dates[@]} >= 7 )); then
            rename_day=$(<"$DST/.cutover-renamed")
            if rescue "$DST/mirror.legacy" "$DST/rescued/legacy-$rename_day" 1; then
                if remove_tree legacy-removal "$DST/mirror.legacy"; then
                    for name in "$DST"/daily-*.tar.gz; do remove_tree legacy-tarball "$name"; done
                    step verification-marker rm -f -- "$DST/.rescue-verification-miss"
                fi
            else
                if [[ -e "$DST/.rescue-verification-miss" ]]; then fail 'rescue verification' 1; fi
                : > "$DST/.rescue-verification-miss"
            fi
        fi
    fi
    if [[ -d "$DST/rescued" && -f "$DST/.rescue-done" && ! -d "$DST/mirror.legacy" ]]; then
        local completed
        completed=$(date -u -d "$(<"$DST/.rescue-done")" +%s)
        if (( now > completed + 30*86400 )); then remove_tree rescued-retention "$DST/rescued"; fi
    fi
    finish
}
main "$@"; exit $?
