#![forbid(unsafe_code)]
// Lint levels do not cross crate boundaries, so the binary root repeats the
// lib's `unwrap_used` deny rather than inheriting it.
#![deny(clippy::unwrap_used)]

use blueline::{agent, ci, cli, mcp, recall, review, shim};

use clap::Parser;

fn main() {
    // The extraction child is dispatched before clap, before the policy loader,
    // before the registry and before the store: it confines itself to one
    // directory and then needs nothing else, and every one of those needs
    // something outside it. It carries no arguments (see
    // `sandbox::child_entrypoint`), so reaching here means this is an ordinary
    // invocation.
    if let Some(code) = blueline::sandbox::child_entrypoint() {
        std::process::exit(code);
    }

    check_for_update();

    if let Err(e) = run() {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}

fn run() -> anyhow::Result<()> {
    let cli = cli::Cli::parse();
    let ecosystem = cli.ecosystem.into();
    let bases = cli::RegistryBases::from_flags(&cli.registry, &cli.index);
    match cli.command {
        cli::Command::Review { pkg, output, yes } => {
            review::run(&pkg, ecosystem, &bases, output, cli.policy.as_deref(), yes)
        }
        cli::Command::Install { pkg, npm_args, yes } => review::install(
            &pkg,
            ecosystem,
            &bases,
            &npm_args,
            cli.policy.as_deref(),
            yes,
        ),
        cli::Command::Ci {
            base,
            lockfile,
            format,
            fail_on,
            output_file,
        } => ci::run(
            &base,
            &lockfile,
            &bases,
            ecosystem,
            cli.policy.as_deref(),
            format.to_ci_format(),
            fail_on,
            output_file.as_deref(),
        ),
        cli::Command::Mcp => mcp::run_stdio(&bases, cli.policy.as_deref()),
        cli::Command::Agent { action } => match action {
            cli::AgentAction::Review { pkg } => {
                agent::run(&pkg, ecosystem, &bases, cli.policy.as_deref())
            }
            cli::AgentAction::Gate { command, format } => agent::gate(
                command.as_deref(),
                format.into(),
                &bases,
                cli.policy.as_deref(),
            ),
        },
        cli::Command::Recall { action } => match action {
            cli::RecallAction::Sync { url } => {
                let synced = recall::sync(&url)?;
                println!(
                    "synced recall snapshot: sequence {}, {} revocations",
                    synced.snapshot.sequence,
                    synced.snapshot.revocations.len()
                );
                Ok(())
            }
            cli::RecallAction::Serve { port, snapshot } => recall::serve(port, &snapshot),
            cli::RecallAction::ExportCandidates { out, limit } => {
                let store = blueline::store::BaselineStore::open()?;
                let candidates: Vec<blueline::store::AuditEntry> = store
                    .audit_entries(limit)?
                    .into_iter()
                    .filter(|e| {
                        matches!(e.action.as_str(), "hold" | "agent_gate" | "agent_review")
                            || e.verdict == "BLOCK"
                            || e.verdict == "HIGH"
                    })
                    .collect();
                let json = serde_json::to_string_pretty(&candidates)?;
                std::fs::write(&out, json)
                    .map_err(|e| anyhow::anyhow!("writing {}: {e}", out.display()))?;
                println!(
                    "exported {} candidate(s) to {}",
                    candidates.len(),
                    out.display()
                );
                Ok(())
            }
            cli::RecallAction::ExportOsv { snapshot, out } => {
                let n = blueline::recall::export_osv(&snapshot, &out)?;
                println!("exported {n} OSV document(s) to {}", out.display());
                Ok(())
            }
        },
        cli::Command::Shim { action } => match action {
            cli::ShimAction::Install { managers, dir } => shim::install(&managers, dir.as_deref()),
            cli::ShimAction::Uninstall { managers, dir } => {
                shim::uninstall(&managers, dir.as_deref())
            }
        },
    }
}

fn check_for_update() {
    if cfg!(test) {
        return;
    }
    if std::env::var("BLUEDISABLE_UPDATE_CHECK").is_ok() {
        return;
    }
    let agent = ureq::AgentBuilder::new()
        .timeout(std::time::Duration::from_secs(2))
        .build();
    let resp = match agent.get("https://crates.io/api/v1/crates/blueline").call() {
        Ok(r) => r,
        Err(_) => return,
    };
    let body = match resp.into_string() {
        Ok(s) => s,
        Err(_) => return,
    };
    let json: serde_json::Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(_) => return,
    };
    let latest = match json
        .get("crate")
        .and_then(|c| c.get("max_version"))
        .and_then(|v| v.as_str())
    {
        Some(s) => s,
        None => return,
    };
    let current = env!("CARGO_PKG_VERSION");
    if let (Ok(latest_v), Ok(current_v)) = (
        semver::Version::parse(latest),
        semver::Version::parse(current),
    ) && latest_v > current_v
    {
        eprintln!(
            "blueline: a newer version ({latest}) is available; run `cargo install blueline` to update"
        );
    }
}
