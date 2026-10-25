use anyhow::{Context, Result, ensure};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::{fs, os::unix::fs::PermissionsExt};

pub struct Config {
    home: PathBuf,
    hosts: Value,
    projects: Value,
}

pub fn load(home: &Path) -> Result<Config> {
    ensure!(
        cfg!(any(target_os = "linux", target_os = "macos")),
        "only native Linux and macOS clients are supported"
    );
    let directory = home.join(".config/remote-agent");
    Ok(Config {
        home: home.to_owned(),
        hosts: read_map(&directory.join("remote-hosts.json"))?,
        projects: read_map(&directory.join("remote-projects.json"))?,
    })
}

fn read_map(path: &Path) -> Result<Value> {
    let value: Value = serde_json::from_slice(
        &fs::read(path).with_context(|| format!("read map {}", path.display()))?,
    )
    .with_context(|| format!("parse map {}", path.display()))?;
    ensure!(
        value.is_object(),
        "map must be a JSON object: {}",
        path.display()
    );
    Ok(value)
}

fn field<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    let text = value[key]
        .as_str()
        .with_context(|| format!("missing or invalid {key}"))?;
    ensure!(!text.is_empty() && !text.contains('\0'), "invalid {key}");
    Ok(text)
}

fn executable(path: &Path) -> bool {
    path.is_absolute()
        && fs::metadata(path).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

fn quote(text: &str) -> Result<String> {
    ensure!(!text.contains('\0'), "remote argument contains NUL");
    Ok(format!("'{}'", text.replace('\'', "'\\''")))
}

impl Config {
    pub fn load(home: &Path) -> Result<Self> {
        load(home)
    }

    pub fn remote_project(&self, project: &str) -> Result<(PathBuf, PathBuf)> {
        let entry = self
            .projects
            .get(project)
            .with_context(|| format!("unknown project: {project}"))?;
        let cwd = self.home.join(field(entry, "remote")?);
        ensure!(
            cwd.is_dir(),
            "project directory does not exist: {}",
            cwd.display()
        );
        let pi = if entry.get("pi_executable").is_some() {
            let pi = PathBuf::from(field(entry, "pi_executable")?);
            ensure!(
                executable(&pi),
                "pi_executable must name an absolute executable regular file: {}",
                pi.display()
            );
            pi
        } else {
            self.find_pi()?
        };
        Ok((cwd, pi))
    }

    pub fn cancel_hook(&self, project: &str) -> Result<Option<PathBuf>> {
        let entry = self
            .projects
            .get(project)
            .with_context(|| format!("unknown project: {project}"))?;
        if entry.get("pi_cancel_executable").is_none() {
            return Ok(None);
        }
        let hook = PathBuf::from(field(entry, "pi_cancel_executable")?);
        ensure!(
            executable(&hook),
            "pi_cancel_executable must name an absolute executable regular file: {}",
            hook.display()
        );
        Ok(Some(hook))
    }

    pub fn launch_environment(&self, host: &str) -> Result<BTreeMap<String, String>> {
        let profile = self
            .hosts
            .get(host)
            .with_context(|| format!("unknown host: {host}"))?;
        let jobs = field(profile, "cargo_build_jobs")?;
        ensure!(
            jobs.bytes().all(|byte| byte.is_ascii_digit())
                && jobs.parse::<usize>().is_ok_and(|value| value > 0),
            "cargo_build_jobs for host {host} must be a positive integer string"
        );
        let mut environment = BTreeMap::from([("CARGO_BUILD_JOBS".into(), jobs.into())]);
        if let Some(value) = profile.get("build_host_scripts") {
            let scripts = value
                .as_str()
                .context("build_host_scripts must be a string")?;
            ensure!(
                !scripts.contains('\0'),
                "build_host_scripts must not contain NUL"
            );
            environment.insert("BUILD_HOST_SCRIPTS".into(), scripts.into());
        }
        Ok(environment)
    }

    pub fn find_pi(&self) -> Result<PathBuf> {
        let mut directories = vec![self.home.join(".local/bin"), self.home.join(".cargo/bin")];
        if let Some(path) = std::env::var_os("PATH") {
            directories.extend(std::env::split_paths(&path));
        }
        directories
            .into_iter()
            .map(|p| p.join("pi"))
            .find(|p| executable(p))
            .context("Pi executable not found")
    }

    pub fn list_projects(&self) -> Value {
        self.projects.clone()
    }
    pub fn state_root(&self) -> PathBuf {
        self.home.join(".local/state/remote-agent-rs")
    }

    pub fn default_host(&self) -> Result<String> {
        ensure!(
            self.hosts.get("agent-server").is_some(),
            "default host agent-server is not configured; select a host explicitly"
        );
        Ok("agent-server".into())
    }

    pub fn ssh_target(&self, host: &str) -> Result<String> {
        let profile = self
            .hosts
            .get(host)
            .with_context(|| format!("unknown host: {host}"))?;
        ensure!(
            profile["windows_shell"].is_null() || profile["windows_shell"] == false,
            "Windows hosts are unsupported"
        );
        ensure!(
            profile["wrap"].is_null() || profile["wrap"].as_array().is_some_and(Vec::is_empty),
            "wrapped hosts are unsupported"
        );
        if let Some(platform) = profile.get("platform").or_else(|| profile.get("os")) {
            ensure!(platform == "linux", "remote server must use Linux");
        }
        let override_key = format!("REMOTE_AGENT_{}_SSH", host.to_uppercase().replace('-', "_"));
        let target = std::env::var(&override_key).unwrap_or(field(profile, "ssh")?.to_owned());
        ensure!(
            !target.is_empty()
                && !target.starts_with('-')
                && !target.chars().any(char::is_whitespace)
                && !target.contains('\0'),
            "invalid SSH destination"
        );
        Ok(target)
    }

    pub fn build_ssh_command(
        &self,
        host: &str,
        user: Option<&str>,
        args: &[String],
    ) -> Result<Command> {
        let target = self.ssh_target(host)?;
        let profile = &self.hosts[host];
        let mut remote = Vec::new();
        let home = PathBuf::from(field(profile, "home")?);
        ensure!(home.is_absolute(), "remote home must be absolute");
        remote.push(
            home.join(".local/bin/remote-agent-rs")
                .to_str()
                .context("remote home is not UTF-8")?
                .into(),
        );
        if let Some(user) = user {
            ensure!(
                !user.is_empty()
                    && user
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
                    && !user.starts_with('-'),
                "invalid target account"
            );
            remote.extend(["__as-user".to_owned(), user.into()]);
        }
        remote.extend_from_slice(args);
        let remote = remote
            .iter()
            .map(|arg| quote(arg))
            .collect::<Result<Vec<_>>>()?
            .join(" ");
        let mut command = Command::new("ssh");
        command.arg(target).arg(remote);
        Ok(command)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};
    use tempfile::TempDir;

    fn fixture() -> TempDir {
        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join(".config/remote-agent");
        fs::create_dir_all(&config).unwrap();
        fs::write(config.join("remote-hosts.json"), r#"{"agent-server":{"ssh":"operator@example","home":"/srv/operator","wrap":[],"windows_shell":false},"desktop":{"ssh":"desktop","home":"/home/test","wrap":["wsl.exe"],"windows_shell":true}}"#).unwrap();
        fs::write(
            config.join("remote-projects.json"),
            r#"{"demo":{"remote":"Projects/demo","local":"/ignored","extra":true}}"#,
        )
        .unwrap();
        fs::create_dir_all(dir.path().join("Projects/demo")).unwrap();
        fs::create_dir_all(dir.path().join(".local/bin")).unwrap();
        let pi = dir.path().join(".local/bin/pi");
        fs::write(&pi, "#!/bin/sh\nprintf '%s\\n' \"$@\"\n").unwrap();
        fs::set_permissions(pi, fs::Permissions::from_mode(0o700)).unwrap();
        dir
    }

    fn write_fixture_map(home: &Path, name: &str, value: Value) {
        fs::write(
            home.join(".config/remote-agent").join(name),
            serde_json::to_vec(&value).unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn launch_environment_selects_host_and_preserves_exact_values() {
        let home = fixture();
        write_fixture_map(
            home.path(),
            "remote-hosts.json",
            serde_json::json!({
                "agent-server": {"cargo_build_jobs": "0008", "build_host_scripts": "/srv/build scripts:$HOME"},
                "other": {"cargo_build_jobs": "2"},
                "empty-scripts": {"cargo_build_jobs": "1", "build_host_scripts": ""}
            }),
        );
        let config = load(home.path()).unwrap();
        assert_eq!(
            config.launch_environment("agent-server").unwrap(),
            std::collections::BTreeMap::from([
                ("CARGO_BUILD_JOBS".into(), "0008".into()),
                (
                    "BUILD_HOST_SCRIPTS".into(),
                    "/srv/build scripts:$HOME".into()
                )
            ])
        );
        let environment = config.launch_environment("other").unwrap();
        assert_eq!(
            environment,
            std::collections::BTreeMap::from([("CARGO_BUILD_JOBS".into(), "2".into())])
        );
        let output = Command::new("/bin/sh")
            .args(["-c", "printf '%s' \"$BUILD_HOST_SCRIPTS\""])
            .env("BUILD_HOST_SCRIPTS", "caller-value")
            .envs(environment)
            .output()
            .unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, b"caller-value");
        assert_eq!(
            config.launch_environment("empty-scripts").unwrap()["BUILD_HOST_SCRIPTS"],
            ""
        );
        assert!(
            config
                .launch_environment("missing")
                .unwrap_err()
                .to_string()
                .contains("unknown host")
        );
    }

    #[test]
    fn launch_environment_rejects_missing_or_invalid_host_settings() {
        let home = fixture();
        for profile in [
            serde_json::json!({}),
            serde_json::json!({"cargo_build_jobs": null}),
            serde_json::json!({"cargo_build_jobs": 4}),
            serde_json::json!({"cargo_build_jobs": ""}),
            serde_json::json!({"cargo_build_jobs": "0"}),
            serde_json::json!({"cargo_build_jobs": "-1"}),
            serde_json::json!({"cargo_build_jobs": "1.5"}),
            serde_json::json!({"cargo_build_jobs": " 2"}),
            serde_json::json!({"cargo_build_jobs": "2\0"}),
            serde_json::json!({"cargo_build_jobs": "999999999999999999999999999999999999999"}),
        ] {
            write_fixture_map(
                home.path(),
                "remote-hosts.json",
                serde_json::json!({"selected": profile}),
            );
            let error = load(home.path())
                .unwrap()
                .launch_environment("selected")
                .unwrap_err();
            assert!(error.to_string().contains("cargo_build_jobs"), "{error}");
        }
        for scripts in [
            serde_json::json!(null),
            serde_json::json!(42),
            serde_json::json!([]),
            serde_json::json!("a\0b"),
        ] {
            write_fixture_map(
                home.path(),
                "remote-hosts.json",
                serde_json::json!({"selected": {"cargo_build_jobs": "4", "build_host_scripts": scripts}}),
            );
            let error = load(home.path())
                .unwrap()
                .launch_environment("selected")
                .unwrap_err();
            assert!(error.to_string().contains("build_host_scripts"), "{error}");
        }
    }

    #[test]
    fn cancel_hook_selects_executable_and_allows_absence() {
        let home = fixture();
        let hook = home.path().join(".local/bin/pi");
        write_fixture_map(
            home.path(),
            "remote-projects.json",
            serde_json::json!({
                "with-hook": {"pi_cancel_executable": hook},
                "without-hook": {}
            }),
        );
        let config = load(home.path()).unwrap();
        let resolved = config.cancel_hook("with-hook").unwrap().unwrap();
        assert_eq!(resolved, hook);
        let output = Command::new(resolved)
            .arg("recorded-conversation")
            .output()
            .unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, b"recorded-conversation\n");
        assert_eq!(config.cancel_hook("without-hook").unwrap(), None);
        assert!(
            config
                .cancel_hook("unknown")
                .unwrap_err()
                .to_string()
                .contains("unknown project")
        );
    }

    #[test]
    fn cancel_hook_rejects_invalid_values_without_default_pi_fallback() {
        let home = fixture();
        let non_executable = home.path().join("hook");
        fs::write(&non_executable, "#!/bin/sh\n").unwrap();
        fs::set_permissions(&non_executable, fs::Permissions::from_mode(0o600)).unwrap();
        for value in [
            serde_json::json!(null),
            serde_json::json!(7),
            serde_json::json!(""),
            serde_json::json!("pi"),
            serde_json::json!("relative/hook"),
            serde_json::json!(home.path().join("missing")),
            serde_json::json!(non_executable),
            serde_json::json!(home.path()),
            serde_json::json!("/invalid\0hook"),
        ] {
            write_fixture_map(
                home.path(),
                "remote-projects.json",
                serde_json::json!({"demo": {"pi_cancel_executable": value}}),
            );
            let error = load(home.path()).unwrap().cancel_hook("demo").unwrap_err();
            assert!(
                error.to_string().contains("pi_cancel_executable"),
                "{error}"
            );
        }
    }

    #[test]
    fn resolves_project_without_changing_map_bytes() {
        let home = fixture();
        let map = home
            .path()
            .join(".config/remote-agent/remote-projects.json");
        let bytes = fs::read(&map).unwrap();
        let config = load(home.path()).unwrap();
        assert_eq!(
            config.remote_project("demo").unwrap(),
            (
                home.path().join("Projects/demo"),
                home.path().join(".local/bin/pi")
            )
        );
        assert_eq!(config.list_projects()["demo"]["extra"], true);
        assert_eq!(
            config.state_root(),
            home.path().join(".local/state/remote-agent-rs")
        );
        assert_eq!(config.default_host().unwrap(), "agent-server");
        assert_eq!(fs::read(map).unwrap(), bytes);
        assert!(config.remote_project("missing").is_err());
    }

    #[test]
    fn missing_maps_and_invalid_explicit_pi_fail() {
        let empty = tempfile::tempdir().unwrap();
        assert!(load(empty.path()).is_err());
        let home = fixture();
        let map = home
            .path()
            .join(".config/remote-agent/remote-projects.json");
        for pi in ["relative/pi", "/no/such/pi", ""] {
            fs::write(
                &map,
                serde_json::to_vec(
                    &serde_json::json!({"demo":{"remote":"Projects/demo","pi_executable":pi}}),
                )
                .unwrap(),
            )
            .unwrap();
            assert!(load(home.path()).unwrap().remote_project("demo").is_err());
        }
    }

    #[test]
    fn exposes_configured_ssh_target_for_network_resolution() {
        let home = fixture();
        let config = load(home.path()).unwrap();
        assert_eq!(
            config.ssh_target("agent-server").unwrap(),
            "operator@example"
        );
        assert!(config.ssh_target("missing").is_err());
        assert!(config.ssh_target("desktop").is_err());
    }

    #[test]
    fn routes_accounts_and_quotes_remote_arguments() {
        let home = fixture();
        let config = load(home.path()).unwrap();
        let args = vec!["__rpc".into(), "a'b; $(touch nope)".into()];
        let command = config
            .build_ssh_command("agent-server", Some("alice"), &args)
            .unwrap();
        assert_eq!(command.get_program(), "ssh");
        let argv: Vec<_> = command.get_args().map(|s| s.to_str().unwrap()).collect();
        assert_eq!(argv[0], "operator@example");
        let output = Command::new("/bin/sh")
            .args(["-c", &format!("set -- {}; printf '%s\\n' \"$@\"", argv[1])])
            .output()
            .unwrap();
        assert!(output.status.success());
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            "/srv/operator/.local/bin/remote-agent-rs\n__as-user\nalice\n__rpc\na'b; $(touch nope)\n"
        );
        let normal = config.build_ssh_command("agent-server", None, &[]).unwrap();
        assert_eq!(
            normal.get_args().nth(1).unwrap(),
            "'/srv/operator/.local/bin/remote-agent-rs'"
        );
        assert!(config.build_ssh_command("desktop", None, &args).is_err());
        assert!(config.build_ssh_command("missing", None, &args).is_err());
        assert!(
            config
                .build_ssh_command("agent-server", Some("../root"), &args)
                .is_err()
        );
    }
}
