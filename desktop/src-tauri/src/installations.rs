//! Find other copies of this macOS application without scanning the disk.
use core_foundation::{
    array::{CFArray, CFArrayRef},
    base::TCFType,
    bundle::CFBundle,
    string::{CFString, CFStringRef},
    url::CFURL,
};
use serde::Serialize;
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
};
use tauri::{AppHandle, WebviewWindow};

// Launch Services is available on the supported macOS 11 release.
#[link(name = "CoreServices", kind = "framework")]
extern "C" {
    fn LSCopyApplicationURLsForBundleIdentifier(
        identifier: CFStringRef,
        error: *mut core_foundation::error::CFErrorRef,
    ) -> CFArrayRef;
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Installation {
    path: PathBuf,
    version: Option<String>,
    running_copy: bool,
}

fn registered_paths(identifier: &str) -> Vec<PathBuf> {
    let identifier = CFString::new(identifier);
    // Launch Services returns an owned array of CFURL objects when it finds matches.
    let urls = unsafe {
        LSCopyApplicationURLsForBundleIdentifier(
            identifier.as_concrete_TypeRef(),
            std::ptr::null_mut(),
        )
    };
    if urls.is_null() {
        return Vec::new();
    }
    let urls = unsafe { CFArray::<CFURL>::wrap_under_create_rule(urls) };
    urls.iter().filter_map(|url| url.to_path()).collect()
}

fn installations(identifier: &str) -> Vec<Installation> {
    let Some(current) = CFBundle::main_bundle()
        .bundle_url()
        .and_then(|url| url.to_path())
    else {
        return Vec::new();
    };
    if current.extension().and_then(|ext| ext.to_str()) != Some("app") {
        return Vec::new();
    }
    let current = current.canonicalize().unwrap_or(current);
    let mut candidates = registered_paths(identifier);
    candidates.push(current.clone());
    candidates.push(PathBuf::from("/Applications/Kinora.app"));
    if let Some(home) = std::env::var_os("HOME") {
        candidates.push(PathBuf::from(home).join("Applications/Kinora.app"));
    }

    let mut copies = matching_bundles(candidates, &current, identifier);
    copies.sort_by_key(|copy| !copy.running_copy);
    copies
}

fn matching_bundles(
    candidates: Vec<PathBuf>,
    current: &Path,
    identifier: &str,
) -> Vec<Installation> {
    let paths: BTreeSet<_> = candidates
        .into_iter()
        .filter_map(|path| fs::canonicalize(path).ok())
        .collect();
    paths
        .into_iter()
        .filter_map(|path| {
            let running_copy = path == current;
            let in_trash = path.components().any(|part| {
                matches!(part.as_os_str().to_str(), Some(".Trash" | ".Trashes"))
            });
            if path.extension().and_then(|ext| ext.to_str()) != Some("app")
                || (in_trash && !running_copy)
            {
                return None;
            }
            let info = plist::Value::from_file(path.join("Contents/Info.plist")).ok()?;
            let info = info.as_dictionary()?;
            if info.get("CFBundleIdentifier")?.as_string()? != identifier {
                return None;
            }
            let version = info
                .get("CFBundleShortVersionString")
                .and_then(plist::Value::as_string)
                .map(str::to_owned);
            Some(Installation { path, version, running_copy })
        })
        .collect()
}

#[tauri::command]
pub(crate) async fn list_installations(
    app: AppHandle,
    window: WebviewWindow,
) -> Result<Vec<Installation>, &'static str> {
    if window.label() != "main" {
        return Err("请在 Kinora 主窗口检查本机安装");
    }
    let identifier = app.config().identifier.clone();
    tauri::async_runtime::spawn_blocking(move || installations(&identifier))
        .await
        .map_err(|_| "暂时无法检查本机安装，请重试")
}

#[tauri::command]
pub(crate) async fn reveal_installation(
    app: AppHandle,
    window: WebviewWindow,
    path: PathBuf,
) -> Result<(), &'static str> {
    if window.label() != "main" {
        return Err("请在 Kinora 主窗口查看本机安装");
    }
    let identifier = app.config().identifier.clone();
    tauri::async_runtime::spawn_blocking(move || {
        // Recheck against Launch Services and the on-disk bundle before opening Finder.
        if !installations(&identifier).iter().any(|copy| copy.path == path) {
            return Err("该副本已移动，请重新检查");
        }
        Command::new("/usr/bin/open")
            .arg("-R")
            .arg(path)
            .status()
            .ok()
            .filter(|status| status.success())
            .map(|_| ())
            .ok_or("无法在 Finder 中显示该副本")
    })
    .await
    .map_err(|_| "无法打开 Finder")?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn identifies_distinct_copies_and_skips_stale_registrations() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let identifier = "com.motionfarm.desktop";
        let bundle = |name: &str, bundle_id: &str, version: &str| {
            let path = root.join(name);
            let contents = path.join("Contents");
            fs::create_dir_all(&contents).unwrap();
            let info = plist::Dictionary::from_iter([
                ("CFBundleIdentifier".to_owned(), plist::Value::from(bundle_id)),
                ("CFBundleShortVersionString".to_owned(), plist::Value::from(version)),
            ]);
            plist::Value::Dictionary(info)
                .to_file_xml(contents.join("Info.plist"))
                .unwrap();
            path
        };
        let current = bundle("Applications/Kinora.app", identifier, "0.10.1");
        let old = bundle("Downloads/Kinora Copy.app", identifier, "0.4.8");
        let alias = root.join("Alias.app");
        symlink(&current, &alias).unwrap();
        let other = bundle("Other/Kinora.app", "com.motionfarm.desktop.dev", "0.10.1");
        let trashed = bundle(".Trash/Kinora.app", identifier, "0.4.7");
        let candidates = vec![
            current.clone(), old.clone(), alias, other, trashed, root.join("Missing.app"),
        ];
        let copies = matching_bundles(candidates.clone(), &current, identifier);
        assert_eq!(copies.len(), 2);
        assert!(copies.iter().any(|copy| copy.path == current && copy.running_copy));
        assert!(copies.iter().any(|copy| copy.path == old && !copy.running_copy
            && copy.version.as_deref() == Some("0.4.8")));

        fs::rename(&old, root.join(".Trash/Kinora Copy.app")).unwrap();
        let remaining = matching_bundles(candidates, &current, identifier);
        assert_eq!(remaining.len(), 1);
        assert!(remaining[0].running_copy);
    }
}
