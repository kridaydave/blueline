//! Shared fixtures for the unit-test modules. One copy so test setups
//! cannot drift apart.

use crate::registry::{Checksum, ChecksumAlg};
use sha2::{Digest, Sha512};

/// Deterministic valid sha512 checksum derived from `tag`.
pub(crate) fn checksum(tag: &str) -> Checksum {
    let mut hasher = Sha512::new();
    hasher.update(tag.as_bytes());
    Checksum {
        alg: ChecksumAlg::Sha512,
        value_hex: format!("{:x}", hasher.finalize()),
    }
}

/// Decode stderr from a command output for test assertions.
pub(crate) fn stderr_of(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

/// Test helper: whether a verdict contains a finding with the given rule ID.
pub(crate) fn has_finding(verdict: &crate::verdict::Verdict, rule_id: &str) -> bool {
    verdict.findings.iter().any(|f| f.rule_id == rule_id)
}
