#!/usr/bin/env bash
#
# verify.sh — local gate runner for blueline.
#
# Runs the core Rust/doc gates with the same command lines CI enforces where
# practical. CI also runs actionlint, RustSec+deny in the audit job, PR
# dogfood, and 32-shard diff-based mutation testing with CI-specific excludes
# and timeouts; local `--mutants` is a narrower/manual check unless you mirror
# CI args.
#
#   ./scripts/verify.sh            fmt + clippy + test + references — the gate
#   ./scripts/verify.sh --fast     same as above
#   ./scripts/verify.sh --mutants  the gate, then mutation testing (best effort)
#   ./scripts/verify.sh --fuzz     the gate, then a libFuzzer smoke per target
#   ./scripts/verify.sh --all      the gate, mutants, plus the cargo-deny supply-chain audit
#
# Works from any cwd: the repo root is resolved from this script's location.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Mutation targets: derived from the repo, never a hand-maintained list.
# Keep in sync with .github/workflows/ci.yml (BLUELINE_MUTANTS_SKIP and
# target globs). lib.rs and error.rs are pure declarations.
MUTANTS_SKIP="src/lib.rs
src/error.rs"
# CI carries a long exclude regex for unbuildable/true-equivalent/fork-bomb
# mutants. Local runs use the same excludes for parity when --mutants is used.
MUTANTS_EXCLUDE='with .*Default::default\(\)|replace && with \|\| in .*|delete ! in (spawn_child|may_spawn_child|may_spawn_child_given|export_osv|check_for_update)|replace may_spawn_child(_given)? -> bool with true|replace check_for_update with \(\)|replace <= with > in evaluate_candidates|replace == with != in scan_file|replace < with <= in levenshtein_at_most_one|replace < with > in levenshtein_at_most_one|replace \+= with \*= in levenshtein_at_most_one|replace > with < in levenshtein_at_most_one|replace > with == in check_for_update|replace > with >= in check_for_update|replace > with < in check_for_update|replace set_file_perm with \(\)|replace set_dir_perm with \(\)'

RUN_MUTANTS=0
RUN_FUZZ=0
RUN_ALL=0

usage() {
    cat <<'EOF'
usage: scripts/verify.sh [--fast] [--mutants] [--fuzz] [--all]

  (default)  fmt + clippy + test + references — the gate
  --fast     same as default
  --mutants  the gate, then mutation testing (best effort)
  --fuzz     the gate, then a libFuzzer smoke per target (60 s each; override with BLUELINE_FUZZ_SECS)
  --all      the gate, mutants, plus the cargo-deny supply-chain audit
EOF
}

for arg in "$@"; do
    case "$arg" in
        --fast)    ;;  # default stage set; accepted for symmetry
        --mutants) RUN_MUTANTS=1 ;;
        --fuzz)    RUN_FUZZ=1 ;;
        --all)     RUN_MUTANTS=1; RUN_ALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *)         echo "verify.sh: unknown argument '$arg'" >&2; usage >&2; exit 2 ;;
    esac
done

declare -a PASSED=()

stage() {
    local name="$1"; local priority="$2"; local category="$3"; local ci_job="$4"; local fix_hint="$5"; shift 5
    printf '\n\033[1m==> [%s %s] %s\033[0m\n    ci_job: %s\n    $ %s\n\n' "$priority" "$category" "$name" "$ci_job" "$*"
    if "$@"; then
        PASSED+=("$priority $category $name")
    else
        local rc=$?
        printf '\n\033[1;31mFAIL\033[0m  [%s %s] %s (exit %d)\n' "$priority" "$category" "$name" "$rc" >&2
        printf '      category: %s\n' "$category" >&2
        printf '      priority: %s\n' "$priority" >&2
        printf '      ci_job:   %s\n' "$ci_job" >&2
        printf '      stage:    %s\n' "$name" >&2
        printf '      cmd:      %s\n' "$*" >&2
        printf '      fix_first: %s\n' "$fix_hint" >&2
        exit "$rc"
    fi
}

stage_mutants() {
    if ! cargo mutants --version >/dev/null 2>&1; then
        echo "cargo-mutants not installed. CI installs it via taiki-e/install-action;" >&2
        echo "locally: cargo install cargo-mutants --locked" >&2
        return 127
    fi
    local files=()
    mapfile -t files < <(git ls-files 'src/*.rs' 'src/registry/*.rs' \
        | grep -vxFf <(printf '%s\n' "$MUTANTS_SKIP"))
    if [ "${#files[@]}" -eq 0 ]; then
        echo "no mutation targets resolved from git ls-files" >&2
        return 1
    fi
    echo "mutating ${#files[@]} files (local/manual; CI uses 32-shard --in-diff runs, timeout 300s, and the same excludes)"
    local args=() f
    for f in "${files[@]}"; do args+=(--file "$f"); done
    local exclude
    exclude="$(cat scripts/mutants_exclude.re)"
    cargo mutants "${args[@]}" --exclude-re "$exclude" --in-place --baseline=skip --timeout 300
}

stage_deny() {
    if ! cargo deny --version >/dev/null 2>&1; then
        echo "cargo-deny not installed: cargo install cargo-deny --locked" >&2
        return 127
    fi
    cargo deny check
}

# Same invocation as the fuzz-smoke job in ci.yml: nightly toolchain (cargo-fuzz
# builds with sanitizer flags that need it), the committed seed corpus, a short
# budget per target. The nightly soak owns the long runs; this is the smoke.
stage_fuzz() {
    if ! cargo fuzz --version >/dev/null 2>&1; then
        echo "cargo-fuzz not installed. CI installs it via taiki-e/install-action;" >&2
        echo "locally: cargo install cargo-fuzz --locked" >&2
        return 127
    fi
    if ! rustup toolchain list | grep -q '^nightly'; then
        echo "nightly toolchain not installed (cargo-fuzz needs it): rustup toolchain install nightly --profile minimal" >&2
        return 127
    fi
    local secs="${BLUELINE_FUZZ_SECS:-60}"
    # Run against a throwaway copy of the committed seeds: libFuzzer writes
    # every grown input back into its corpus dir, and the committed corpus is
    # a reviewed floor, not a scratch space (the nightly soak owns growth).
    local rc=0
    FUZZ_CORPUS_DIR="$(mktemp -d)"
    trap 'rm -rf "$FUZZ_CORPUS_DIR"' EXIT
    for target in safe_extract read_package_json pypi_version aur_version pkgbuild_tokenizer install_ref_scan; do
        rm -rf "$FUZZ_CORPUS_DIR/$target"
        cp -r "fuzz/corpus/$target" "$FUZZ_CORPUS_DIR/$target"
        # gnu, not cargo-fuzz's musl default: ASan is incompatible with musl's
        # statically linked libc. Same invocation as the CI fuzz-smoke job.
        if ! cargo +nightly fuzz run --target x86_64-unknown-linux-gnu "$target" "$FUZZ_CORPUS_DIR/$target" -- \
            -max_total_time="$secs" -rss_limit_mb=4096 -timeout=25; then
            rc=1
        fi
    done
    return "$rc"
}

stages="fmt, clippy, test, references"
[ "$RUN_MUTANTS" -eq 1 ] && stages="$stages, mutants"
[ "$RUN_FUZZ" -eq 1 ] && stages="$stages, fuzz-smoke"
[ "$RUN_ALL" -eq 1 ] && stages="$stages, cargo-deny"

echo "repo:      $REPO_ROOT"
echo "toolchain: $(rustc --version 2>/dev/null || echo 'rustc not on PATH')"
echo "stages:    $stages"
echo "note: CI also runs P2 gate.workflow.lint (actionlint), RustSec as part of P1 gate.security.audit, PR dogfood gates (P3/P4), and 32-shard diff-based mutation testing (P1). Local runs do not fully mirror PR/push mutation sharding."

stage "Format" "P0" "gate.rust.format" "fmt" \
    "Run cargo fmt --all, then re-run verify.sh." \
    cargo fmt --all -- --check
stage "Clippy" "P0" "gate.rust.lint" "lint" \
    "Fix clippy errors/warnings; do not proceed past static lint failures." \
    cargo clippy --all-targets --locked -- -D warnings
if command -v cargo-nextest >/dev/null 2>&1; then
    echo "test runner: cargo-nextest (CI uses nextest --test-threads 8)"
    stage "Tests" "P0" "gate.rust.test" "test" \
        "Fix failing tests before lower-priority gates." \
        cargo nextest run --all-targets --locked --test-threads 8
else
    echo "test runner: cargo test (CI uses cargo-nextest --test-threads 8)"
    stage "Tests" "P0" "gate.rust.test" "test" \
        "Fix failing tests before lower-priority gates." \
        cargo test --all-targets --locked
fi

if [ "$RUN_ALL" -eq 1 ]; then
    stage "Supply-chain audit" "P1" "gate.security.audit" "audit" \
        "Review cargo-deny output (advisories/licenses/bans/sources). CI also runs RustSec." \
        stage_deny
fi

if [ "$RUN_MUTANTS" -eq 1 ]; then
    stage "Mutation testing" "P1" "gate.security.mutation" "mutants-local (CI: mutants-pr/mutants, 32 shards, --in-diff)" \
        "Inspect cargo-mutants output/survivors; local mutants are not identical to CI's 32-shard in-diff runs." \
        stage_mutants
fi

if [ "$RUN_FUZZ" -eq 1 ]; then
    stage "Fuzz smoke" "P1" "gate.security.fuzz" "fuzz-smoke" \
        "Investigate fuzz crashes/artifacts; fuzz smoke is best-effort locally but part of CI for parse/extract diffs." \
        stage_fuzz
fi

stage "References" "P2" "gate.docs.references" "references" \
    "Repair broken references/citations in references/." \
    scripts/check-references.sh

printf '\n\033[1;32mPASS\033[0m  %s\n' "$(IFS=, ; echo "${PASSED[*]}")"
