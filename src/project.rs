//! Project checkout creation and mapping shared by the client and the remote account.
use crate::control;
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Map, Value, json};
use std::fs;
use std::path::{Component, Path};
use std::process::Command;

pub fn validate_name(project: &str) -> Result<()> {
    ensure!(
        !project.is_empty()
            && project.len() <= 128
            && project.as_bytes()[0].is_ascii_alphanumeric()
            && project
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-'),
        "project name must contain 1–128 ASCII letters, digits, hyphens or underscores and start with a letter or digit"
    );
    Ok(())
}

pub fn validate_local_path(local: &str) -> Result<()> {
    ensure!(
        !local.contains('\0') && Path::new(local).is_absolute(),
        "local path must be an absolute path without NUL bytes: {local}"
    );
    Ok(())
}

pub fn validate_remote_path(remote: &str) -> Result<()> {
    ensure!(
        !remote.is_empty() && !remote.contains('\0'),
        "remote path is empty or contains NUL bytes"
    );
    ensure!(
        Path::new(remote)
            .components()
            .all(|component| matches!(component, Component::Normal(_))),
        "remote path must be relative to the account home without '.', '..' or a root: {remote}"
    );
    Ok(())
}

/// Create a checkout with an initialized Git repository. Existing paths are never reused.
pub fn create_checkout(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(_) => bail!("path already exists: {}", path.display()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(error).context(format!("inspect checkout path {}", path.display()));
        }
    }
    fs::create_dir_all(path).with_context(|| format!("create checkout {}", path.display()))?;
    if let Err(error) = initialize_git(path) {
        let _ = fs::remove_dir_all(path);
        return Err(error);
    }
    Ok(())
}

fn initialize_git(path: &Path) -> Result<()> {
    let output = Command::new("git")
        .args(["init", "-q", "-b", "master"])
        .arg(path)
        .output()
        .with_context(|| format!("run git init in {}", path.display()))?;
    ensure!(
        output.status.success(),
        "git init failed in {}: {}{}",
        path.display(),
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}

/// Report whether the mapping is already present. A different mapping under the same
/// project key is an error; nothing is overwritten.
pub fn registered(
    projects: &Map<String, Value>,
    project: &str,
    local: &str,
    remote: &str,
) -> Result<bool> {
    validate_name(project)?;
    validate_local_path(local)?;
    validate_remote_path(remote)?;
    let Some(entry) = projects.get(project) else {
        return Ok(false);
    };
    ensure!(
        entry.get("local") == Some(&Value::String(local.to_owned()))
            && entry.get("remote") == Some(&Value::String(remote.to_owned())),
        "project {project} is already mapped to {entry}; remove its map entry to change it"
    );
    Ok(true)
}

/// Add a mapping to the private map file. Returns false when it was already present.
pub fn register(
    map_path: &Path,
    projects: &mut Map<String, Value>,
    project: &str,
    local: &str,
    remote: &str,
) -> Result<bool> {
    if registered(projects, project, local, remote)? {
        return Ok(false);
    }
    projects.insert(
        project.to_owned(),
        json!({"local": local, "remote": remote}),
    );
    control::write_private_json(map_path, &Value::Object(projects.clone()))?;
    Ok(true)
}

/// Create the remote checkout and mapping for a client `create` request.
pub fn create_remote(
    home: &Path,
    map_path: &Path,
    projects: &mut Map<String, Value>,
    project: &str,
    local: &str,
    remote: &str,
) -> Result<Value> {
    validate_remote_path(remote)?;
    let path = home.join(remote);
    if registered(projects, project, local, remote)? {
        ensure!(
            path.is_dir(),
            "mapped remote path does not exist: {}",
            path.display()
        );
        return Ok(reply(project, local, &path, false));
    }
    create_checkout(&path)?;
    if let Err(error) = register(map_path, projects, project, local, remote) {
        let _ = fs::remove_dir_all(&path);
        return Err(error);
    }
    Ok(reply(project, local, &path, true))
}

fn reply(project: &str, local: &str, remote: &Path, created: bool) -> Value {
    json!({
        "project": project,
        "local": local,
        "remote": remote.to_string_lossy(),
        "created": created,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;
    use tempfile::TempDir;

    fn map_fixture(dir: &TempDir) -> PathBuf {
        let map = dir.path().join(".config/remote-agent/remote-projects.json");
        fs::create_dir_all(map.parent().unwrap()).unwrap();
        fs::write(
            &map,
            r#"{"engine":{"local":"/src/engine","remote":"Projects/engine"}}"#,
        )
        .unwrap();
        map
    }

    fn projects(map: &Path) -> Map<String, Value> {
        serde_json::from_slice::<Value>(&fs::read(map).unwrap())
            .unwrap()
            .as_object()
            .unwrap()
            .clone()
    }

    fn is_git_checkout(path: &Path) -> bool {
        path.join(".git").is_dir()
    }

    #[test]
    fn validation_rejects_names_and_paths_that_could_escape_the_mapping() {
        for project in ["", "with space", "a/b", "..", ".hidden", "a\0b"] {
            assert!(validate_name(project).is_err(), "{project:?}");
        }
        for project in ["creatures", "wow-ui-sim", "server2"] {
            validate_name(project).unwrap();
        }
        for local in ["relative/path", "", "a\0b"] {
            assert!(validate_local_path(local).is_err(), "{local:?}");
        }
        validate_local_path("/home/dev/Projects/demo").unwrap();
        for remote in ["", "/absolute", "../outside", "Projects/../outside", "a\0b"] {
            assert!(validate_remote_path(remote).is_err(), "{remote:?}");
        }
        for remote in ["Projects/demo", "Projects", "demo"] {
            validate_remote_path(remote).unwrap();
        }
    }

    #[test]
    fn create_checkout_initializes_master_and_refuses_existing_paths() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("demo");
        create_checkout(&path).unwrap();
        assert!(is_git_checkout(&path));
        let head = fs::read_to_string(path.join(".git/HEAD")).unwrap();
        assert_eq!(head.trim(), "ref: refs/heads/master");
        let error = create_checkout(&path).unwrap_err();
        assert!(error.to_string().contains("already exists"), "{error}");
    }

    #[test]
    fn register_adds_a_private_mapping_and_keeps_existing_projects() {
        let dir = tempfile::tempdir().unwrap();
        let map = map_fixture(&dir);
        let mut entries = projects(&map);
        assert!(register(&map, &mut entries, "demo", "/src/demo", "Projects/demo").unwrap());
        assert!(!register(&map, &mut entries, "demo", "/src/demo", "Projects/demo").unwrap());

        let text = fs::read_to_string(&map).unwrap();
        let stored: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(
            stored["demo"],
            json!({"local": "/src/demo", "remote": "Projects/demo"})
        );
        assert_eq!(stored["engine"]["remote"], "Projects/engine");
        assert_eq!(
            fs::metadata(&map).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[test]
    fn register_refuses_a_conflicting_mapping_without_touching_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let map = map_fixture(&dir);
        let before = fs::read(&map).unwrap();
        let mut entries = projects(&map);
        let error = register(
            &map,
            &mut entries,
            "engine",
            "/src/other",
            "Projects/engine",
        )
        .unwrap_err();
        assert!(error.to_string().contains("already mapped"), "{error}");
        assert_eq!(fs::read(&map).unwrap(), before);
    }

    #[test]
    fn create_remote_builds_the_checkout_and_mapping_once() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path();
        let map = map_fixture(&dir);
        let mut entries = projects(&map);

        let reply = create_remote(
            home,
            &map,
            &mut entries,
            "demo",
            "/src/demo",
            "Projects/demo",
        )
        .unwrap();
        assert_eq!(reply["created"], true);
        assert!(is_git_checkout(&home.join("Projects/demo")));
        assert_eq!(
            projects(&map)["demo"],
            json!({"local": "/src/demo", "remote": "Projects/demo"})
        );

        let again = create_remote(
            home,
            &map,
            &mut entries,
            "demo",
            "/src/demo",
            "Projects/demo",
        )
        .unwrap();
        assert_eq!(again["created"], false);

        let conflict = create_remote(
            home,
            &map,
            &mut entries,
            "engine",
            "/src/other",
            "Projects/engine",
        )
        .unwrap_err();
        assert!(
            conflict.to_string().contains("already mapped"),
            "{conflict}"
        );
        let unregistered = create_remote(
            home,
            &map,
            &mut entries,
            "fresh",
            "/src/fresh",
            "Projects/demo",
        )
        .unwrap_err();
        assert!(
            unregistered.to_string().contains("already exists"),
            "{unregistered}"
        );
        assert!(!projects(&map).contains_key("fresh"));
    }
}
