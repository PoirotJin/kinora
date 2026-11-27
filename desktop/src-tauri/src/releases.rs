//! Bundled release notes remain available offline after an update.
use std::path::Path;
use tauri::{AppHandle, Manager};

pub fn notes() -> serde_json::Value {
    serde_json::from_str(include_str!("../release-notes.json")).expect("validated at build time")
}

fn needs_ack(path: &Path, version: &str) -> bool {
    std::fs::read_to_string(path).map(|saved| saved.trim() != version).unwrap_or(true)
}

pub fn unread(app: &AppHandle) -> bool {
    app.path().app_local_data_dir().map(|dir| needs_ack(&dir.join("release-seen.txt"), &app.package_info().version.to_string())).unwrap_or(true)
}

#[tauri::command]
pub fn acknowledge_release(app: AppHandle, version: String) -> Result<(), String> {
    if version != app.package_info().version.to_string() { return Err("版本不匹配".into()); }
    let dir = app.path().app_local_data_dir().map_err(|_| "无法读取应用目录")?;
    std::fs::create_dir_all(&dir).map_err(|_| "无法创建应用目录")?;
    let temp = dir.join("release-seen.tmp");
    std::fs::write(&temp, version).and_then(|_| std::fs::rename(temp, dir.join("release-seen.txt"))).map_err(|_| "无法保存阅读状态".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn acknowledgement_survives_relaunch_but_not_new_version() {
        let dir = std::env::temp_dir().join(format!("mf-release-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("seen");
        assert!(needs_ack(&path, "0.4.2"));
        std::fs::write(&path, "0.4.2").unwrap();
        assert!(!needs_ack(&path, "0.4.2"));
        assert!(needs_ack(&path, "0.4.3"));
        std::fs::write(&path, "broken").unwrap();
        assert!(needs_ack(&path, "0.4.2"));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
