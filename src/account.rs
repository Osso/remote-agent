use anyhow::{Context, Result, ensure};
use std::{
    ffi::{CStr, CString, OsStr, OsString},
    mem::MaybeUninit,
    os::unix::ffi::OsStrExt,
    path::PathBuf,
    process::Command,
};

#[derive(Debug)]
pub struct Account {
    pub uid: libc::uid_t,
    pub gid: libc::gid_t,
    pub name: String,
    pub home: PathBuf,
    pub shell: PathBuf,
}

pub fn read_account(name: &str) -> Result<Account> {
    let name = CString::new(name).context("account name contains NUL")?;
    let mut buffer = vec![0_u8; 1024];
    loop {
        let mut passwd = MaybeUninit::<libc::passwd>::uninit();
        let mut result = std::ptr::null_mut();
        // getpwnam_r writes passwd and pointers into buffer; copy fields before buffer drops.
        let status = unsafe {
            libc::getpwnam_r(
                name.as_ptr(),
                passwd.as_mut_ptr(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut result,
            )
        };
        if status == libc::ERANGE {
            let size = buffer
                .len()
                .checked_mul(2)
                .context("passwd entry too large")?;
            buffer.resize(size, 0);
            continue;
        }
        if status != 0 {
            return Err(std::io::Error::from_raw_os_error(status))
                .context("cannot read account from passwd database");
        }
        ensure!(
            !result.is_null(),
            "account not found: {}",
            name.to_string_lossy()
        );
        // A successful lookup with non-null result initializes passwd and its string fields.
        let passwd = unsafe { passwd.assume_init() };
        let account = unsafe {
            Account {
                uid: passwd.pw_uid,
                gid: passwd.pw_gid,
                name: CStr::from_ptr(passwd.pw_name)
                    .to_str()
                    .context("account name is not UTF-8")?
                    .to_owned(),
                home: PathBuf::from(OsStr::from_bytes(CStr::from_ptr(passwd.pw_dir).to_bytes())),
                shell: PathBuf::from(OsStr::from_bytes(
                    CStr::from_ptr(passwd.pw_shell).to_bytes(),
                )),
            }
        };
        ensure!(account.home.is_absolute(), "account home must be absolute");
        return Ok(account);
    }
}

pub fn build_user_command(account: &Account, args: &[String]) -> Result<Command> {
    ensure!(
        matches!(args.first().map(String::as_str), Some("__rpc" | "__login")),
        "target account operation must be __rpc or __login"
    );
    let home = &account.home;
    let runtime = format!("/run/user/{}", account.uid);
    let mut path = home.join(".local/bin").into_os_string();
    path.push(":");
    path.push(home.join(".cargo/bin"));
    path.push(":/usr/local/bin:/usr/bin:/bin");
    let source_lang = std::env::var("LANG").ok();
    let environment: [(&str, OsString); 13] = [
        ("HOME", home.as_os_str().to_owned()),
        ("USER", account.name.clone().into()),
        ("LOGNAME", account.name.clone().into()),
        ("SHELL", account.shell.as_os_str().to_owned()),
        ("PATH", path),
        ("XDG_CONFIG_HOME", home.join(".config").into_os_string()),
        ("XDG_DATA_HOME", home.join(".local/share").into_os_string()),
        ("XDG_STATE_HOME", home.join(".local/state").into_os_string()),
        ("XDG_RUNTIME_DIR", runtime.clone().into()),
        (
            "DBUS_SESSION_BUS_ADDRESS",
            format!("unix:path={runtime}/bus").into(),
        ),
        ("CARGO_HOME", home.join(".cargo").into_os_string()),
        (
            "PI_CODING_AGENT_DIR",
            home.join(".config/pi/agent").into_os_string(),
        ),
        ("LANG", safe_lang(source_lang.as_deref()).into()),
    ];
    let mut command = Command::new("/usr/bin/authsudo");
    command.args(["-u", &account.name, "/usr/bin/env", "-i"]);
    for (key, value) in environment {
        let mut assignment = OsString::from(format!("{key}="));
        assignment.push(value);
        command.arg(assignment);
    }
    command
        .arg(home.join(".local/bin/remote-agent-rs"))
        .args(args);
    Ok(command)
}

fn safe_lang(value: Option<&str>) -> &str {
    value
        .filter(|value| {
            !value.is_empty()
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"_.@-".contains(&byte))
        })
        .unwrap_or("C.UTF-8")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{collections::BTreeMap, fs, os::unix::fs::PermissionsExt};

    #[test]
    fn reads_current_native_passwd_account() {
        let uid = unsafe { libc::geteuid() };
        let output = Command::new("/usr/bin/getent")
            .args(["passwd", &uid.to_string()])
            .output()
            .unwrap();
        assert!(output.status.success());
        let passwd = String::from_utf8(output.stdout).unwrap();
        let fields: Vec<_> = passwd.trim_end().split(':').collect();
        let account = read_account(fields[0]).unwrap();
        assert_eq!(account.name, fields[0]);
        assert_eq!(account.uid, uid);
        assert_eq!(account.gid.to_string(), fields[3]);
        assert_eq!(account.home, PathBuf::from(fields[5]));
        assert_eq!(account.shell, PathBuf::from(fields[6]));
    }

    #[test]
    fn rejects_missing_or_nul_account() {
        let error = read_account("remote-agent-account-that-does-not-exist").unwrap_err();
        assert!(error.to_string().contains("not found"), "{error}");
        assert!(read_account("bad\0name").is_err());
    }

    #[test]
    fn rejects_operations_other_than_rpc_or_login() {
        let account = fixture_account(PathBuf::from("/srv/example"));
        for args in [vec![], vec!["/bin/sh".into()], vec!["__as-user".into()]] {
            assert!(build_user_command(&account, &args).is_err());
        }
        for operation in ["__rpc", "__login"] {
            assert!(build_user_command(&account, &[operation.into()]).is_ok());
        }
    }

    fn fixture_account(home: PathBuf) -> Account {
        Account {
            uid: unsafe { libc::geteuid() },
            gid: unsafe { libc::getegid() },
            name: "fixture-account".into(),
            home,
            shell: "/bin/sh".into(),
        }
    }

    #[test]
    fn executes_clean_target_environment_through_owned_provider() {
        let directory = tempfile::tempdir().unwrap();
        let account = fixture_account(directory.path().join("nonstandard-home"));
        let worker = account.home.join(".local/bin/remote-agent-rs");
        fs::create_dir_all(worker.parent().unwrap()).unwrap();
        fs::write(&worker, "#!/bin/sh\n[ \"$1\" = __rpc ] && [ \"$2\" = \"a'b; $(literal)\" ] || exit 12\nunset PWD\nexec /usr/bin/env -0\n".replace("$(literal)", "\\$(literal)")).unwrap();
        fs::set_permissions(&worker, fs::Permissions::from_mode(0o700)).unwrap();
        let provider = directory.path().join("authsudo-fixture");
        fs::write(&provider, "#!/bin/sh\n[ \"$1\" = -u ] && [ \"$2\" = fixture-account ] || exit 13\nshift 2\nexec \"$@\"\n").unwrap();
        fs::set_permissions(&provider, fs::Permissions::from_mode(0o700)).unwrap();
        let command =
            build_user_command(&account, &["__rpc".into(), "a'b; $(literal)".into()]).unwrap();
        assert_eq!(command.get_program(), "/usr/bin/authsudo");
        let output = Command::new(provider)
            .args(command.get_args())
            .env("REMOTE_AGENT_ACCOUNT_PROBE", "primary-context")
            .env("PRIMARY_SECRET", "must-not-reach-target")
            .env("HOME", "/home/primary")
            .env("USER", "primary")
            .env("LOGNAME", "primary")
            .env("XDG_RUNTIME_DIR", "/run/user/primary-context")
            .env("DBUS_SESSION_BUS_ADDRESS", "primary")
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        let environment: BTreeMap<_, _> = output
            .stdout
            .split(|byte| *byte == 0)
            .filter(|entry| !entry.is_empty())
            .map(|entry| String::from_utf8(entry.to_vec()).unwrap())
            .map(|entry| {
                let (key, value) = entry.split_once('=').unwrap();
                (key.to_owned(), value.to_owned())
            })
            .collect();
        let home = account.home.to_str().unwrap();
        let runtime = format!("/run/user/{}", account.uid);
        let expected = BTreeMap::from([
            ("HOME".into(), home.into()),
            ("USER".into(), account.name.clone()),
            ("LOGNAME".into(), account.name.clone()),
            ("SHELL".into(), "/bin/sh".into()),
            (
                "PATH".into(),
                format!("{home}/.local/bin:{home}/.cargo/bin:/usr/local/bin:/usr/bin:/bin"),
            ),
            ("XDG_CONFIG_HOME".into(), format!("{home}/.config")),
            ("XDG_DATA_HOME".into(), format!("{home}/.local/share")),
            ("XDG_STATE_HOME".into(), format!("{home}/.local/state")),
            ("XDG_RUNTIME_DIR".into(), runtime.clone()),
            (
                "DBUS_SESSION_BUS_ADDRESS".into(),
                format!("unix:path={runtime}/bus"),
            ),
            ("CARGO_HOME".into(), format!("{home}/.cargo")),
            (
                "PI_CODING_AGENT_DIR".into(),
                format!("{home}/.config/pi/agent"),
            ),
            (
                "LANG".into(),
                safe_lang(std::env::var("LANG").ok().as_deref()).into(),
            ),
        ]);
        assert_eq!(environment, expected);
    }

    #[test]
    fn locale_accepts_only_safe_lang_values() {
        for value in ["en_US.UTF-8", "C.UTF-8", "C", "fr_FR.utf8@euro"] {
            assert_eq!(safe_lang(Some(value)), value);
        }
        for value in [
            None,
            Some(""),
            Some("a=b"),
            Some("bad\nvalue"),
            Some("$(id)"),
        ] {
            assert_eq!(safe_lang(value), "C.UTF-8");
        }
    }
}
