#!/bin/bash
# Shared-state failures need a foreground re-check before attribution.
attr_shared() {
  awk '
    $2 == "NEW:" && $3 != "none" {
      for (i = 3; i <= NF; i++) {
        if ($i == "UNCHECKED") continue
        if (!($i in first)) first[$i] = $1
        else if (first[$i] != $1) {
          shared[first[$i]] = 1
          shared[$1] = 1
        }
      }
    }
    END { for (spec in shared) print spec }
  ' "$1" | sort -n -u
}

attr_resolve() {
  local attr_out=$1 attr_fn=$2 attr_spec attr_tmp attr_count=0
  local -a attr_specs
  # Snapshot before replacing lines: later passes must not change the cohort.
  mapfile -t attr_specs < <(attr_shared "$attr_out")
  for attr_spec in "${attr_specs[@]}"; do
    attr_count=$((attr_count + 1))
    attr_tmp=$(mktemp "${attr_out}.XXXXXX") || return
    if ! awk -v spec="$attr_spec" -v rerun="$((attr_count <= 6))" '
      $1 == spec {
        if (rerun) next
        if ($NF != "UNCHECKED") $0 = $0 " UNCHECKED"
      }
      { print }
    ' "$attr_out" > "$attr_tmp" || ! cat "$attr_tmp" > "$attr_out"; then
      rm -f "$attr_tmp"
      return 1
    fi
    rm -f "$attr_tmp" || return
    if ((attr_count <= 6)); then
      "$attr_fn" "$attr_spec" || return
    fi
  done
}
