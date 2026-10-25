//! A bounded disk window of decoded terminal rows and authoritative screen snapshots.
use std::{
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use crate::protocol::DisplayUpdate;

#[derive(Serialize, Deserialize)]
#[serde(tag = "record", content = "update", rename_all = "snake_case")]
enum Record<T> {
    Checkpoint(T),
    Delta(T),
}

pub struct HistoryStore {
    path: PathBuf,
    file: File,
    budget: usize,
    length: usize,
    expired: bool,
}

impl HistoryStore {
    pub fn open(path: &Path, budget: usize) -> Result<Self> {
        ensure!(budget > 0, "history disk budget must be positive");
        let file = OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .mode(0o600)
            .open(path)
            .with_context(|| format!("open history {}", path.display()))?;
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .with_context(|| format!("make history private {}", path.display()))?;
        let length = usize::try_from(file.metadata()?.len())?;
        ensure!(
            length <= budget,
            "existing history {} exceeds disk budget {budget}",
            path.display()
        );
        let expired = if length == 0 {
            false
        } else {
            read(path)?.expired
        };
        Ok(Self {
            path: path.to_owned(),
            file,
            budget,
            length,
            expired,
        })
    }

    /// `snapshot` must describe the terminal after `update`, including all retained rows.
    /// Only persistence is trimmed; neither `update` nor the terminal is modified.
    pub fn append(
        &mut self,
        update: &DisplayUpdate,
        snapshot: impl FnOnce() -> DisplayUpdate,
    ) -> Result<()> {
        if self.length == 0 || update.reset {
            return self.checkpoint(snapshot(), !update.reset && self.expired);
        }
        let bytes = encode_line(&Record::Delta(update))?;
        if bytes.len() > self.budget - self.length {
            return self.checkpoint(snapshot(), self.expired);
        }
        if let Err(error) = self.file.write_all(&bytes) {
            self.file
                .set_len(self.length as u64)
                .with_context(|| format!("rollback failed history append after {error}"))?;
            return Err(error).with_context(|| format!("append history {}", self.path.display()));
        }
        self.length += bytes.len();
        self.expired |= update.expired;
        Ok(())
    }

    fn checkpoint(&mut self, mut snapshot: DisplayUpdate, expired: bool) -> Result<()> {
        snapshot.reset = true;
        snapshot.expired |= expired;
        trim_checkpoint(&mut snapshot, self.budget).with_context(|| {
            format!(
                "history {} disk budget {}",
                self.path.display(),
                self.budget
            )
        })?;
        let bytes = encode_line(&Record::Checkpoint(&snapshot))?;
        let file = replace_private(&self.path, &bytes)?;
        self.file = file;
        self.length = bytes.len();
        self.expired = snapshot.expired;
        Ok(())
    }
}

fn encode_line<T: Serialize>(record: &Record<T>) -> Result<Vec<u8>> {
    let mut bytes = serde_json::to_vec(record).context("encode history record")?;
    bytes.push(b'\n');
    Ok(bytes)
}

fn trim_checkpoint(snapshot: &mut DisplayUpdate, budget: usize) -> Result<()> {
    let mut rows = std::mem::take(&mut snapshot.scrolled);
    let base_length = encode_line(&Record::Checkpoint(&*snapshot))?.len();
    ensure!(
        base_length <= budget,
        "current terminal state alone needs {base_length} bytes, budget is {budget}"
    );
    let mut length = base_length;
    let mut keep_from = rows.len();
    for row in rows.iter().rev() {
        // Base64 uses four bytes per three input bytes; add quotes and a separator.
        let row_length = row.len().div_ceil(3) * 4 + 2 + usize::from(keep_from < rows.len());
        if row_length > budget - length {
            break;
        }
        length += row_length;
        keep_from -= 1;
    }
    if keep_from > 0 {
        rows.drain(..keep_from);
        snapshot.expired = true;
    }
    snapshot.scrolled = rows;
    Ok(())
}

fn replace_private(path: &Path, bytes: &[u8]) -> Result<File> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let temporary = parent.join(format!(".history-{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .read(true)
        .append(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)
        .with_context(|| format!("create private history checkpoint {}", temporary.display()))?;
    let result = (|| -> Result<()> {
        ensure!(
            file.metadata()?.uid() == fs::metadata(path)?.uid(),
            "history checkpoint must preserve owner of {}",
            path.display()
        );
        file.write_all(bytes).context("write history checkpoint")?;
        file.sync_all().context("sync history checkpoint")?;
        fs::rename(&temporary, path)
            .with_context(|| format!("replace history {}", path.display()))?;
        Ok(())
    })();
    if let Err(error) = result {
        fs::remove_file(&temporary)
            .with_context(|| format!("remove failed history checkpoint after {error:#}"))?;
        return Err(error);
    }
    Ok(file)
}

/// Fold complete JSONL records; no terminal escape parsing or byte-tail replay.
/// The result is a full replacement window (`reset = true`).
pub fn read(path: &Path) -> Result<DisplayUpdate> {
    let file = File::open(path).with_context(|| format!("read history {}", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut line = Vec::new();
    let mut state: Option<DisplayUpdate> = None;
    let mut number = 0;
    loop {
        line.clear();
        if reader.read_until(b'\n', &mut line)? == 0 {
            break;
        }
        number += 1;
        ensure!(
            line.last() == Some(&b'\n'),
            "incomplete history record {number} in {}",
            path.display()
        );
        let record: Record<DisplayUpdate> = serde_json::from_slice(&line)
            .with_context(|| format!("decode history record {number} in {}", path.display()))?;
        match record {
            Record::Checkpoint(update) => {
                ensure!(
                    state.is_none(),
                    "unexpected history checkpoint at record {number}"
                );
                state = Some(update);
            }
            Record::Delta(mut update) => {
                let previous = state.as_mut().context("history delta without checkpoint")?;
                if update.reset {
                    *previous = update;
                } else {
                    previous.scrolled.append(&mut update.scrolled);
                    update.scrolled = std::mem::take(&mut previous.scrolled);
                    update.expired |= previous.expired;
                    *previous = update;
                }
            }
        }
    }
    let mut state = state.with_context(|| format!("empty history {}", path.display()))?;
    state.reset = true;
    Ok(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::Size;
    use std::{fs, os::unix::fs::PermissionsExt};

    fn update(sequence: u64, rows: &[&[u8]]) -> DisplayUpdate {
        DisplayUpdate {
            sequence,
            size: Size { rows: 24, cols: 80 },
            scrolled: rows.iter().map(|row| row.to_vec()).collect(),
            screen: vec![0xf0, 0x9f, 0x1b, b'['],
            control: vec![7],
            reset: false,
            expired: false,
        }
    }

    #[test]
    fn deltas_append_without_snapshot_and_restore_order_after_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history");
        let mut store = HistoryStore::open(&path, 4096).unwrap();
        let first = update(1, &[b"first", &[0x98, 0x80]]);
        store.append(&first, || first.clone()).unwrap();
        let prefix = fs::read(&path).unwrap();
        let second = update(2, &[b"third"]);
        store
            .append(&second, || panic!("ordinary append requested snapshot"))
            .unwrap();
        let bytes = fs::read(&path).unwrap();
        assert!(bytes.starts_with(&prefix));
        assert!(bytes.len() > prefix.len());
        assert_eq!(bytes.iter().filter(|b| **b == b'\n').count(), 2);
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        drop(store);
        let mut store = HistoryStore::open(&path, 4096).unwrap();
        let third = update(3, &[b"fourth"]);
        store
            .append(&third, || panic!("reopen requested snapshot"))
            .unwrap();
        let actual = read(&path).unwrap();
        assert_eq!(
            actual.scrolled,
            vec![
                b"first".to_vec(),
                vec![0x98, 0x80],
                b"third".to_vec(),
                b"fourth".to_vec()
            ]
        );
        assert_eq!(actual.sequence, 3);
        assert_eq!(actual.screen, third.screen);
        assert_eq!(actual.control, third.control);
        assert_eq!(actual.size, third.size);
        assert!(actual.reset);
        assert!(!actual.expired);
    }

    #[test]
    fn compaction_keeps_newest_rows_within_disk_budget_and_marks_eviction() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history");
        let budget = 800;
        let mut store = HistoryStore::open(&path, budget).unwrap();
        let mut snapshot = update(0, &[]);
        for sequence in 1..=20 {
            let row = vec![sequence as u8; 90];
            let delta = update(sequence, &[&row]);
            snapshot.sequence = sequence;
            snapshot.scrolled.push(row);
            store.append(&delta, || snapshot.clone()).unwrap();
            assert!(fs::metadata(&path).unwrap().len() <= budget as u64);
            let restored = read(&path).unwrap();
            assert_eq!(restored.sequence, sequence);
            assert!(snapshot.scrolled.ends_with(&restored.scrolled));
        }
        let restored = read(&path).unwrap();
        assert!(restored.expired);
        assert!(!restored.scrolled.is_empty());
        assert!(restored.scrolled.len() < snapshot.scrolled.len());
        assert_eq!(
            snapshot.scrolled.len(),
            20,
            "persistence must not trim network snapshot"
        );
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            fs::read_dir(dir.path()).unwrap().count(),
            1,
            "temporary files leaked"
        );
    }

    #[test]
    fn reset_discards_old_rows_and_clears_expiration() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history");
        let mut store = HistoryStore::open(&path, 4096).unwrap();
        let mut old = update(1, &[b"secret-old-row"]);
        old.expired = true;
        store.append(&old, || old.clone()).unwrap();
        let mut reset = update(2, &[b"new-row"]);
        reset.reset = true;
        store.append(&reset, || reset.clone()).unwrap();
        let actual = read(&path).unwrap();
        assert_eq!(actual.scrolled, reset.scrolled);
        assert!(!actual.expired);
        let next = update(3, &[b"later"]);
        store
            .append(&next, || panic!("delta requested snapshot"))
            .unwrap();
        assert_eq!(
            read(&path).unwrap().scrolled,
            vec![b"new-row".to_vec(), b"later".to_vec()]
        );
    }

    #[test]
    fn oversized_current_state_errors_without_replacing_valid_history() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history");
        let mut store = HistoryStore::open(&path, 600).unwrap();
        let initial = update(1, &[b"retained"]);
        store.append(&initial, || initial.clone()).unwrap();
        let before = fs::read(&path).unwrap();
        let mut huge = update(2, &[b"discardable"]);
        huge.screen = vec![b'x'; 1000];
        let error = store.append(&huge, || huge.clone()).unwrap_err();
        assert!(error.to_string().contains("budget"), "{error:#}");
        assert_eq!(fs::read(&path).unwrap(), before);
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 1);
    }

    #[test]
    fn read_rejects_missing_empty_and_corrupt_records() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history");
        assert!(read(&path).is_err());
        fs::write(&path, b"").unwrap();
        assert!(read(&path).is_err());
        fs::write(&path, b"{broken}\n").unwrap();
        assert!(read(&path).is_err());
        assert!(HistoryStore::open(&path, 4096).is_err());
    }
}
